"""
Sync runner.
Orchestrates:
  1. Load config
  2. Instantiate connector
  3. Provision SharePoint list schema
  4. Fetch data from source API in batches
  5. Upsert to SharePoint
  6. Return a structured result summary
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from core.base_connector import BaseConnector
from sharepoint.client import SharePointClient

logger = logging.getLogger(__name__)


@dataclass
class SyncResult:
    connector_name: str
    list_name: str
    total_fetched: int = 0
    created: int = 0
    updated: int = 0
    failed: int = 0
    duration_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.failed == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "connector": self.connector_name,
            "list_name": self.list_name,
            "total_fetched": self.total_fetched,
            "created": self.created,
            "updated": self.updated,
            "failed": self.failed,
            "duration_seconds": round(self.duration_seconds, 2),
            "success": self.success,
            "errors": self.errors,
        }


class SyncRunner:
    """
    Runs one or many connectors and syncs results to SharePoint.

    Usage:
        runner = SyncRunner(sp_client)
        result = runner.run(my_connector)
    """

    def __init__(self, sp_client: SharePointClient):
        self.sp = sp_client

    def run(self, connector: BaseConnector) -> SyncResult:
        start = time.time()
        connector_name = connector.__class__.__name__
        schema = connector.schema()

        result = SyncResult(
            connector_name=connector_name,
            list_name=schema.list_name,
        )

        logger.info(f"[{connector_name}] Starting sync → '{schema.list_name}'")

        # Step 1 – authenticate
        try:
            connector.authenticate()
        except Exception as exc:
            msg = f"Authentication failed: {exc}"
            logger.error(f"[{connector_name}] {msg}")
            result.errors.append(msg)
            result.duration_seconds = time.time() - start
            return result

        # Step 2 – provision schema
        try:
            self.sp.provision_schema(
                list_name=schema.list_name,
                fields=schema.fields,
                description=f"Auto-provisioned by {connector_name}",
            )
        except Exception as exc:
            msg = f"Schema provisioning failed: {exc}"
            logger.error(f"[{connector_name}] {msg}")
            result.errors.append(msg)
            result.duration_seconds = time.time() - start
            return result

        # Step 3 – fetch + upsert in batches
        try:
            for batch in connector.batched_fetch():
                sp_items = []
                for item in batch:
                    row = dict(item.fields)
                    if item.unique_key:
                        row["__unique_key__"] = item.unique_key
                    sp_items.append(row)

                counts = self.sp.batch_upsert(schema.list_name, sp_items)
                result.total_fetched += len(batch)
                result.created += counts["created"]
                result.updated += counts["updated"]
                result.failed += counts["failed"]

        except Exception as exc:
            msg = f"Fetch/upsert error: {exc}"
            logger.error(f"[{connector_name}] {msg}", exc_info=True)
            result.errors.append(msg)

        result.duration_seconds = time.time() - start
        logger.info(
            f"[{connector_name}] Done. "
            f"fetched={result.total_fetched} "
            f"created={result.created} "
            f"updated={result.updated} "
            f"failed={result.failed} "
            f"({result.duration_seconds:.1f}s)"
        )
        return result

    def run_many(self, connectors: list[BaseConnector]) -> list[SyncResult]:
        return [self.run(c) for c in connectors]
