"""
Generic API connector.
Allows you to define an arbitrary HTTP API endpoint + field mappings
in configuration without writing a new connector class.
Useful for quick onboarding of new data sources.
"""

import json
import logging
from typing import Any, Iterator

import requests

from core.base_connector import BaseConnector, ConnectorResult, ConnectorSchema

logger = logging.getLogger(__name__)


class GenericAPIConnector(BaseConnector):
    """
    Config keys:
        list_name       - Target SharePoint list name
        endpoint        - Full URL of the API endpoint
        method          - HTTP method (default: GET)
        headers         - (optional) dict of request headers
        query_params    - (optional) dict of query parameters
        body            - (optional) request body (dict, will be JSON-encoded)
        auth            - (optional) dict:
                            {"type": "bearer", "token": "..."}
                            {"type": "api_key", "header": "X-API-Key", "value": "..."}
                            {"type": "basic", "username": "...", "password": "..."}
        response_path   - dot-notation path to the list of objects, e.g. "data.items"
                          Leave blank if the response root IS the list.
        pagination      - (optional) dict:
                            {"type": "next_link", "field": "@odata.nextLink"}
                            {"type": "page_param", "param": "page", "page_size_param": "limit", "page_size": 100}
                            {"type": "offset_param", "param": "offset", "limit_param": "limit", "limit": 100}
        unique_key_field - Field in each object to use as the upsert key
        field_map       - {source_field: {"sp_name": "SPColumnName", "sp_type": "Text"}}
                          If omitted, all fields are stored as Text under their original name.
        schema_fields   - Override: {sp_column_name: sp_type} – used when field_map is absent.
    """

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.list_name = config["list_name"]
        self.endpoint = config["endpoint"]
        self.method = config.get("method", "GET").upper()
        self.extra_headers: dict = config.get("headers", {})
        self.query_params: dict = config.get("query_params", {})
        self.body: dict | None = config.get("body")
        self.auth_config: dict | None = config.get("auth")
        self.response_path: str = config.get("response_path", "")
        self.pagination_config: dict | None = config.get("pagination")
        self.unique_key_field: str | None = config.get("unique_key_field")
        self.field_map: dict = config.get("field_map", {})
        self._schema_fields: dict = config.get("schema_fields", {})

    # ------------------------------------------------------------------ #

    def schema(self) -> ConnectorSchema:
        if self._schema_fields:
            return ConnectorSchema(list_name=self.list_name, fields={"UniqueKey": "Text", **self._schema_fields})

        if self.field_map:
            fields = {"UniqueKey": "Text"}
            for source_field, mapping in self.field_map.items():
                fields[mapping["sp_name"]] = mapping.get("sp_type", "Text")
            return ConnectorSchema(list_name=self.list_name, fields=fields)

        # Minimal fallback: just store raw JSON blob
        return ConnectorSchema(list_name=self.list_name, fields={"UniqueKey": "Text", "Data": "Note"})

    def fetch(self) -> Iterator[ConnectorResult]:
        yield from self._paginated_fetch()

    # ------------------------------------------------------------------ #
    # HTTP + pagination                                                    #
    # ------------------------------------------------------------------ #

    def _build_headers(self) -> dict:
        headers = {"Content-Type": "application/json", **self.extra_headers}
        if not self.auth_config:
            return headers
        auth_type = self.auth_config.get("type", "").lower()
        if auth_type == "bearer":
            headers["Authorization"] = f"Bearer {self.auth_config['token']}"
        elif auth_type == "api_key":
            headers[self.auth_config["header"]] = self.auth_config["value"]
        return headers

    def _build_auth(self):
        if not self.auth_config:
            return None
        if self.auth_config.get("type") == "basic":
            return (self.auth_config["username"], self.auth_config["password"])
        return None

    def _get_page(self, url: str, params: dict) -> tuple[list[dict], str | None]:
        resp = requests.request(
            self.method,
            url,
            headers=self._build_headers(),
            params=params,
            json=self.body if self.method != "GET" else None,
            auth=self._build_auth(),
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()

        # Navigate to the data array
        items = data
        if self.response_path:
            for key in self.response_path.split("."):
                if isinstance(items, dict):
                    items = items.get(key, [])
        if not isinstance(items, list):
            items = [items]

        # Extract next page link / token
        next_url = None
        if self.pagination_config and self.pagination_config.get("type") == "next_link":
            field = self.pagination_config.get("field", "")
            next_url = data.get(field) if isinstance(data, dict) else None

        return items, next_url

    def _paginated_fetch(self) -> Iterator[ConnectorResult]:
        pc = self.pagination_config
        url = self.endpoint
        params = dict(self.query_params)

        if not pc:
            items, _ = self._get_page(url, params)
            yield from (self._map_item(i) for i in items)
            return

        ptype = pc.get("type")

        if ptype == "next_link":
            while url:
                items, next_url = self._get_page(url, params)
                yield from (self._map_item(i) for i in items)
                url = next_url
                params = {}

        elif ptype == "page_param":
            page = int(pc.get("start_page", 1))
            size = int(pc.get("page_size", 100))
            while True:
                params[pc.get("param", "page")] = page
                params[pc.get("page_size_param", "limit")] = size
                items, _ = self._get_page(url, params)
                if not items:
                    break
                yield from (self._map_item(i) for i in items)
                if len(items) < size:
                    break
                page += 1

        elif ptype == "offset_param":
            offset = 0
            limit = int(pc.get("limit", 100))
            while True:
                params[pc.get("param", "offset")] = offset
                params[pc.get("limit_param", "limit")] = limit
                items, _ = self._get_page(url, params)
                if not items:
                    break
                yield from (self._map_item(i) for i in items)
                offset += limit
                if len(items) < limit:
                    break

    # ------------------------------------------------------------------ #
    # Field mapping                                                        #
    # ------------------------------------------------------------------ #

    def _map_item(self, item: dict) -> ConnectorResult:
        unique_key = str(item.get(self.unique_key_field, "")) if self.unique_key_field else None

        if self.field_map:
            fields = {}
            for source_field, mapping in self.field_map.items():
                # Support dot-notation for nested source fields
                value = self._get_nested(item, source_field)
                sp_name = mapping["sp_name"]
                fields[sp_name] = value
        else:
            # Dump everything as JSON
            fields = {"Data": json.dumps(item, default=str)}

        return ConnectorResult(unique_key=unique_key, fields=fields)

    @staticmethod
    def _get_nested(obj: dict, path: str) -> Any:
        """Navigate dot-notation path into a nested dict."""
        parts = path.split(".")
        for part in parts:
            if isinstance(obj, dict):
                obj = obj.get(part)
            else:
                return None
        return obj
