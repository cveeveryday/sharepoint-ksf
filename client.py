"""
SharePoint Online client.
Handles:
  - App-only auth via Microsoft Identity (client credentials flow)
  - List creation / schema provisioning
  - Batch upsert of items (create or update based on a unique key column)
"""

import logging
import time
from typing import Any, Optional
from functools import lru_cache

import requests

logger = logging.getLogger(__name__)

# SharePoint REST field-type map
SP_FIELD_TYPES: dict[str, dict] = {
    "Text":       {"odata.type": "SP.FieldText", "FieldTypeKind": 2},
    "Note":       {"odata.type": "SP.FieldMultiLineText", "FieldTypeKind": 3},
    "Number":     {"odata.type": "SP.FieldNumber", "FieldTypeKind": 9},
    "DateTime":   {"odata.type": "SP.FieldDateTime", "FieldTypeKind": 4},
    "Boolean":    {"odata.type": "SP.Field", "FieldTypeKind": 8},
    "Choice":     {"odata.type": "SP.FieldChoice", "FieldTypeKind": 6},
    "URL":        {"odata.type": "SP.FieldUrl", "FieldTypeKind": 11},
    "User":       {"odata.type": "SP.FieldUser", "FieldTypeKind": 20},
    "Lookup":     {"odata.type": "SP.FieldLookup", "FieldTypeKind": 7},
}

GRAPH_SCOPE = "https://graph.microsoft.com/.default"
SP_SCOPE_TEMPLATE = "https://{tenant}.sharepoint.com/.default"


class SharePointClient:
    """
    Thin wrapper around the SharePoint REST v1 API and Microsoft Graph.

    Authentication uses the OAuth2 client-credentials flow (app-only).
    Requires an Entra ID app registration with Sites.ReadWrite.All (or equivalent).
    """

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        site_url: str,              # e.g. https://contoso.sharepoint.com/sites/KPIs
        tenant_name: str,           # e.g. contoso
        retry_attempts: int = 3,
        retry_backoff: float = 2.0,
    ):
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.site_url = site_url.rstrip("/")
        self.tenant_name = tenant_name
        self.retry_attempts = retry_attempts
        self.retry_backoff = retry_backoff

        self._sp_token: Optional[str] = None
        self._sp_token_expiry: float = 0.0

    # ------------------------------------------------------------------ #
    # Authentication                                                       #
    # ------------------------------------------------------------------ #

    def _get_token(self, scope: str) -> str:
        url = f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token"
        data = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": scope,
        }
        resp = requests.post(url, data=data, timeout=30)
        resp.raise_for_status()
        return resp.json()["access_token"]

    def _sp_headers(self) -> dict:
        now = time.time()
        if not self._sp_token or now >= self._sp_token_expiry:
            scope = SP_SCOPE_TEMPLATE.format(tenant=self.tenant_name)
            self._sp_token = self._get_token(scope)
            self._sp_token_expiry = now + 3500  # tokens valid ~1 hour
        return {
            "Authorization": f"Bearer {self._sp_token}",
            "Accept": "application/json;odata=nometadata",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------ #
    # HTTP helpers                                                         #
    # ------------------------------------------------------------------ #

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        for attempt in range(1, self.retry_attempts + 1):
            try:
                resp = requests.request(
                    method,
                    url,
                    headers=self._sp_headers(),
                    timeout=60,
                    **kwargs,
                )
                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", self.retry_backoff * attempt))
                    logger.warning(f"Rate-limited by SharePoint. Waiting {wait}s …")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp
            except requests.HTTPError as exc:
                if attempt == self.retry_attempts:
                    raise
                wait = self.retry_backoff ** attempt
                logger.warning(f"HTTP error on attempt {attempt}: {exc}. Retrying in {wait}s …")
                time.sleep(wait)
        raise RuntimeError("Exceeded retry attempts")  # unreachable but satisfies type checkers

    def _api(self, path: str) -> str:
        return f"{self.site_url}/_api/{path}"

    # ------------------------------------------------------------------ #
    # List management                                                      #
    # ------------------------------------------------------------------ #

    def list_exists(self, list_name: str) -> bool:
        try:
            self._request("GET", self._api(f"web/lists/GetByTitle('{list_name}')"))
            return True
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return False
            raise

    def create_list(self, list_name: str, description: str = "") -> dict:
        """Create a Generic list."""
        payload = {
            "__metadata": {"type": "SP.List"},
            "AllowContentTypes": True,
            "BaseTemplate": 100,
            "ContentTypesEnabled": False,
            "Description": description,
            "Title": list_name,
        }
        resp = self._request("POST", self._api("web/lists"), json=payload)
        logger.info(f"Created SharePoint list: {list_name}")
        return resp.json()

    def add_field(self, list_name: str, field_name: str, field_type: str = "Text") -> None:
        """Add a column to an existing list if it doesn't already exist."""
        # Check if column already exists
        try:
            self._request(
                "GET",
                self._api(f"web/lists/GetByTitle('{list_name}')/fields/GetByInternalNameOrTitle('{field_name}')"),
            )
            logger.debug(f"Field '{field_name}' already exists in '{list_name}' – skipping.")
            return
        except requests.HTTPError:
            pass

        sp_type_def = SP_FIELD_TYPES.get(field_type, SP_FIELD_TYPES["Text"])
        payload = {
            "__metadata": {"type": sp_type_def["odata.type"]},
            "FieldTypeKind": sp_type_def["FieldTypeKind"],
            "Title": field_name,
            "StaticName": field_name,
        }
        self._request(
            "POST",
            self._api(f"web/lists/GetByTitle('{list_name}')/fields"),
            json=payload,
        )
        logger.debug(f"Added field '{field_name}' ({field_type}) to '{list_name}'.")

    def provision_schema(self, list_name: str, fields: dict[str, str], description: str = "") -> None:
        """
        Ensure list exists and all columns are present.
        fields: {column_name: sp_type}
        """
        if not self.list_exists(list_name):
            self.create_list(list_name, description)

        for field_name, field_type in fields.items():
            self.add_field(list_name, field_name, field_type)

        logger.info(f"Schema provisioned for list '{list_name}'.")

    # ------------------------------------------------------------------ #
    # Item operations                                                      #
    # ------------------------------------------------------------------ #

    def get_item_by_unique_key(self, list_name: str, key_value: str) -> Optional[dict]:
        """Find a list item where UniqueKey == key_value (or None)."""
        url = self._api(f"web/lists/GetByTitle('{list_name}')/items")
        params = {
            "$filter": f"UniqueKey eq '{key_value}'",
            "$top": "1",
        }
        resp = self._request("GET", url, params=params)
        items = resp.json().get("value", [])
        return items[0] if items else None

    def create_item(self, list_name: str, fields: dict[str, Any]) -> dict:
        resp = self._request(
            "POST",
            self._api(f"web/lists/GetByTitle('{list_name}')/items"),
            json=fields,
        )
        return resp.json()

    def update_item(self, list_name: str, item_id: int, fields: dict[str, Any]) -> None:
        url = self._api(f"web/lists/GetByTitle('{list_name}')/items({item_id})")
        headers_extra = {
            "If-Match": "*",
            "X-HTTP-Method": "MERGE",
        }
        merged_headers = {**self._sp_headers(), **headers_extra}
        requests.request("POST", url, headers=merged_headers, json=fields, timeout=60).raise_for_status()

    def upsert_item(self, list_name: str, fields: dict[str, Any], unique_key: Optional[str] = None) -> str:
        """
        Create or update a list item.
        Returns 'created' or 'updated'.
        """
        if unique_key:
            existing = self.get_item_by_unique_key(list_name, unique_key)
            if existing:
                self.update_item(list_name, existing["Id"], fields)
                return "updated"

        self.create_item(list_name, fields)
        return "created"

    def batch_upsert(self, list_name: str, items: list[dict]) -> dict[str, int]:
        """
        Upsert a list of items. Each item dict may contain '__unique_key__'.
        Returns counts: {"created": N, "updated": M, "failed": K}
        """
        counts = {"created": 0, "updated": 0, "failed": 0}
        for item in items:
            unique_key = item.pop("__unique_key__", None)
            try:
                result = self.upsert_item(list_name, item, unique_key)
                counts[result] += 1
            except Exception as exc:
                logger.error(f"Failed to upsert item (key={unique_key}): {exc}")
                counts["failed"] += 1
        return counts
