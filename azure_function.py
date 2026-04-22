"""
Azure Functions handler.
Compatible with Azure Functions Python v2 programming model.

Trigger types supported:
  - HTTP trigger (manual / API Gateway equivalent)
  - Timer trigger (scheduled, like EventBridge)

Deploy this file alongside function_app.py (see below).

Required App Settings (equivalent to Lambda env vars):
  SP_TENANT_ID, SP_CLIENT_ID, SP_CLIENT_SECRET, SP_SITE_URL, SP_TENANT_NAME
  AZURE_KEY_VAULT_URL  (optional – if secrets are in Key Vault)
"""

import json
import logging
import os
import sys

import azure.functions as func

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from core.config import Config
from core.runner import SyncRunner
from sharepoint.client import SharePointClient
from connectors.entraid import EntraIDConnector
from connectors.meraki import MerakiConnector
from connectors.sendgrid_ninjaone import SendGridConnector, NinjaOneConnector
from connectors.generic import GenericAPIConnector

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

CONNECTOR_REGISTRY = {
    "entraid":  EntraIDConnector,
    "meraki":   MerakiConnector,
    "sendgrid": SendGridConnector,
    "ninjaone": NinjaOneConnector,
    "generic":  GenericAPIConnector,
}

app = func.FunctionApp()


# ------------------------------------------------------------------ #
# Shared logic (same as Lambda handler, no cloud-specific code)       #
# ------------------------------------------------------------------ #

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
    resolved = {}
    for k, v in raw_config.items():
        if isinstance(v, dict) and "$secret" in v:
            resolved[k] = cfg.require(v["$secret"])
        else:
            resolved[k] = v
    return resolved


def process_jobs(jobs: list[dict]) -> dict:
    cfg = Config()
    results = []

    for job in jobs:
        connector_type = job.get("connector", "").lower()
        raw_cfg = job.get("connector_config", {})
        sp_config_key = job.get("sp_config_key", "default")

        connector_class = CONNECTOR_REGISTRY.get(connector_type)
        if not connector_class:
            results.append({"connector": connector_type, "error": f"Unknown type: {connector_type}"})
            continue

        try:
            resolved = _resolve_connector_config(raw_cfg, cfg)
            connector = connector_class(resolved)
            sp_client = _build_sp_client(cfg, sp_config_key)
            runner = SyncRunner(sp_client)
            result = runner.run(connector)
            results.append(result.to_dict())
        except Exception as exc:
            logger.error(f"Job failed: {exc}", exc_info=True)
            results.append({"connector": connector_type, "error": str(exc)})

    all_ok = all(r.get("success", False) for r in results if "error" not in r)
    return {"results": results, "all_ok": all_ok}


# ------------------------------------------------------------------ #
# HTTP Trigger                                                         #
# ------------------------------------------------------------------ #

@app.route(route="sync", auth_level=func.AuthLevel.FUNCTION, methods=["POST"])
def http_trigger(req: func.HttpRequest) -> func.HttpResponse:
    """
    POST /api/sync
    Body: {"jobs": [...]}
    """
    logger.info("HTTP trigger received.")
    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse("Invalid JSON body.", status_code=400)

    jobs = body.get("jobs", [])
    if not jobs:
        return func.HttpResponse(
            json.dumps({"message": "No jobs provided."}),
            mimetype="application/json",
            status_code=200,
        )

    outcome = process_jobs(jobs)
    status_code = 200 if outcome["all_ok"] else 207
    return func.HttpResponse(
        json.dumps(outcome),
        mimetype="application/json",
        status_code=status_code,
    )


# ------------------------------------------------------------------ #
# Timer Trigger (scheduled sync)                                       #
# ------------------------------------------------------------------ #

@app.timer_trigger(
    schedule=os.getenv("SYNC_CRON", "0 0 * * * *"),  # hourly by default
    arg_name="mytimer",
    run_on_startup=False,
)
def timer_trigger(mytimer: func.TimerRequest) -> None:
    """
    Scheduled sync driven by SYNC_JOBS_JSON environment variable.
    Set SYNC_JOBS_JSON to a JSON-encoded list of job definitions, e.g.:
        [{"connector": "entraid", "connector_config": {...}}]
    """
    logger.info("Timer trigger fired.")
    raw_jobs = os.getenv("SYNC_JOBS_JSON", "[]")
    try:
        jobs = json.loads(raw_jobs)
    except json.JSONDecodeError:
        logger.error("SYNC_JOBS_JSON is not valid JSON – aborting.")
        return

    if not jobs:
        logger.warning("SYNC_JOBS_JSON is empty – no jobs to run.")
        return

    outcome = process_jobs(jobs)
    logger.info(f"Timer sync complete: {json.dumps(outcome, indent=2)}")
