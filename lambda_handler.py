"""
AWS Lambda handler.

Entrypoint: handlers/lambda_handler.py::handler

Expected event structure (from EventBridge / manual invoke):
{
  "jobs": [
    {
      "connector": "entraid",          // connector type key
      "connector_config": { ... },     // connector-specific config
      "sp_config_key": "default"       // optional: which SP config block to use
    },
    ...
  ]
}

All secrets are fetched from AWS Secrets Manager / SSM Parameter Store via core.config.Config.
"""

import json
import logging
import os
import sys

# Allow relative imports when running from project root
sys.path.insert(0, os.path.dirname(__file__))

from config import Config
from runner import SyncRunner
from client import SharePointClient
from entraid import EntraIDConnector
from meraki import MerakiConnector
from sendgrid_ninjaone import SendGridConnector, NinjaOneConnector
from generic import GenericAPIConnector

# Configure logging – Lambda replaces root handler automatically
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

CONNECTOR_REGISTRY = {
    "entraid":   EntraIDConnector,
    "meraki":    MerakiConnector,
    "sendgrid":  SendGridConnector,
    "ninjaone":  NinjaOneConnector,
    "generic":   GenericAPIConnector,
}


def _build_sp_client(config: Config, sp_config_key: str = "default") -> SharePointClient:
    prefix = f"SP_{sp_config_key.upper()}_" if sp_config_key != "default" else "SP_"
    return SharePointClient(
        tenant_id=config.require(f"{prefix}TENANT_ID"),
        client_id=config.require(f"{prefix}CLIENT_ID"),
        client_secret=config.require(f"{prefix}CLIENT_SECRET"),
        site_url=config.require(f"{prefix}SITE_URL"),
        tenant_name=config.require(f"{prefix}TENANT_NAME"),
    )


def _resolve_connector_config(raw_config: dict, cfg: Config) -> dict:
    """
    Replace any value of the form {"$secret": "KEY_NAME"} with the actual
    secret value, allowing you to keep secrets out of the event payload.
    """
    resolved = {}
    for k, v in raw_config.items():
        if isinstance(v, dict) and "$secret" in v:
            resolved[k] = cfg.require(v["$secret"])
        else:
            resolved[k] = v
    return resolved


def handler(event: dict, context) -> dict:
    """AWS Lambda entrypoint."""
    cfg = Config()
    results = []

    jobs = event.get("jobs", [])
    if not jobs:
        logger.warning("No jobs found in event payload.")
        return {"statusCode": 200, "body": json.dumps({"message": "No jobs to process."})}

    for job in jobs:
        connector_type = job.get("connector", "").lower()
        raw_connector_config = job.get("connector_config", {})
        sp_config_key = job.get("sp_config_key", "default")

        connector_class = CONNECTOR_REGISTRY.get(connector_type)
        if not connector_class:
            msg = f"Unknown connector type: '{connector_type}'. Available: {list(CONNECTOR_REGISTRY)}"
            logger.error(msg)
            results.append({"connector": connector_type, "error": msg})
            continue

        try:
            resolved_config = _resolve_connector_config(raw_connector_config, cfg)
            connector = connector_class(resolved_config)
            sp_client = _build_sp_client(cfg, sp_config_key)
            runner = SyncRunner(sp_client)
            result = runner.run(connector)
            results.append(result.to_dict())
        except Exception as exc:
            logger.error(f"Job failed for connector '{connector_type}': {exc}", exc_info=True)
            results.append({"connector": connector_type, "error": str(exc)})

    all_ok = all(r.get("success", False) for r in results if "error" not in r)
    status_code = 200 if all_ok else 207  # 207 Multi-Status if some jobs failed

    return {
        "statusCode": status_code,
        "body": json.dumps({"results": results}),
    }


# ------------------------------------------------------------------ #
# Local development entry point                                        #
# ------------------------------------------------------------------ #
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run sync jobs locally")
    parser.add_argument("--event", type=str, help="Path to a JSON event file")
    args = parser.parse_args()

    if args.event:
        with open(args.event) as f:
            test_event = json.load(f)
    else:
        test_event = {"jobs": []}

    result = handler(test_event, None)
    print(json.dumps(json.loads(result["body"]), indent=2))
