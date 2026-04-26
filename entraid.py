"""
Entra ID (Azure AD) connector.
Fetches users, devices, groups, or sign-in logs via Microsoft Graph API.
"""

import logging
from typing import Any, Iterator

import requests

from base_connector import BaseConnector, ConnectorResult, ConnectorSchema

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class EntraIDConnector(BaseConnector):
    """
    Config keys (passed via the config dict):
        tenant_id       - Entra ID tenant GUID
        client_id       - App registration client ID
        client_secret   - App registration client secret
        resource        - One of: "users", "devices", "groups", "signInLogs"
        select_fields   - (optional) comma-separated Graph $select fields
        filter_query    - (optional) OData $filter string
        list_name       - Target SharePoint list name
    """

    RESOURCE_SCHEMAS: dict[str, dict[str, str]] = {
        "users": {
            "UniqueKey":         "Text",
            "DisplayName":       "Text",
            "UserPrincipalName": "Text",
            "Mail":              "Text",
            "Department":        "Text",
            "JobTitle":          "Text",
            "AccountEnabled":    "Boolean",
            "CreatedDateTime":   "DateTime",
            "LastSignInDateTime":"DateTime",
            "LicenseStatus":     "Text",
        },
        "devices": {
            "UniqueKey":          "Text",
            "DisplayName":        "Text",
            "OperatingSystem":    "Text",
            "OperatingSystemVersion": "Text",
            "TrustType":          "Text",
            "IsCompliant":        "Boolean",
            "IsManaged":          "Boolean",
            "RegisteredDateTime": "DateTime",
            "ApproximateLastSignInDateTime": "DateTime",
        },
        "groups": {
            "UniqueKey":       "Text",
            "DisplayName":     "Text",
            "Description":     "Text",
            "GroupTypes":      "Text",
            "Mail":            "Text",
            "MemberCount":     "Number",
            "CreatedDateTime": "DateTime",
        },
        "signInLogs": {
            "UniqueKey":       "Text",
            "UserDisplayName": "Text",
            "UserPrincipalName": "Text",
            "AppDisplayName":  "Text",
            "IPAddress":       "Text",
            "Status":          "Text",
            "CreatedDateTime": "DateTime",
            "Location":        "Text",
            "ClientAppUsed":   "Text",
        },
    }

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.resource = config.get("resource", "users")
        self.select = config.get("select_fields")
        self.filter_query = config.get("filter_query")
        self.list_name = config.get("list_name", f"EntraID_{self.resource.capitalize()}")
        self._token: str | None = None

    # ------------------------------------------------------------------ #
    # BaseConnector implementation                                         #
    # ------------------------------------------------------------------ #

    def schema(self) -> ConnectorSchema:
        fields = self.RESOURCE_SCHEMAS.get(self.resource, {"UniqueKey": "Text", "Data": "Note"})
        return ConnectorSchema(list_name=self.list_name, fields=fields)

    def authenticate(self) -> None:
        url = f"https://login.microsoftonline.com/{self.config['tenant_id']}/oauth2/v2.0/token"
        data = {
            "grant_type": "client_credentials",
            "client_id": self.config["client_id"],
            "client_secret": self.config["client_secret"],
            "scope": "https://graph.microsoft.com/.default",
        }
        resp = requests.post(url, data=data, timeout=30)
        resp.raise_for_status()
        self._token = resp.json()["access_token"]
        logger.info(f"EntraID authenticated for resource: {self.resource}")

    def fetch(self) -> Iterator[ConnectorResult]:
        if not self._token:
            self.authenticate()

        headers = {"Authorization": f"Bearer {self._token}"}
        params: dict[str, str] = {"$top": "999"}

        if self.select:
            params["$select"] = self.select
        elif self.resource in self.RESOURCE_SCHEMAS:
            # Build $select from our schema keys (minus UniqueKey which is our internal field)
            schema_keys = [k for k in self.RESOURCE_SCHEMAS[self.resource] if k != "UniqueKey"]
            params["$select"] = ",".join([k[0].lower() + k[1:] for k in schema_keys])

        if self.filter_query:
            params["$filter"] = self.filter_query

        # Special handling for signInLogs (beta endpoint)
        base = "https://graph.microsoft.com/v1.0"
        if self.resource == "signInLogs":
            base = "https://graph.microsoft.com/v1.0"

        url: str | None = f"{base}/{self.resource}"

        while url:
            resp = requests.get(url, headers=headers, params=params, timeout=60)
            resp.raise_for_status()
            data = resp.json()

            for item in data.get("value", []):
                yield self._map(item)

            # Handle Graph pagination
            url = data.get("@odata.nextLink")
            params = {}  # nextLink already has query params embedded

    # ------------------------------------------------------------------ #
    # Mapping helpers                                                      #
    # ------------------------------------------------------------------ #

    def _map(self, item: dict[str, Any]) -> ConnectorResult:
        mapper = {
            "users":      self._map_user,
            "devices":    self._map_device,
            "groups":     self._map_group,
            "signInLogs": self._map_signin,
        }.get(self.resource, self._map_generic)
        return mapper(item)

    def _map_user(self, u: dict) -> ConnectorResult:
        return ConnectorResult(
            unique_key=u.get("id"),
            fields={
                "DisplayName":       u.get("displayName", ""),
                "UserPrincipalName": u.get("userPrincipalName", ""),
                "Mail":              u.get("mail", ""),
                "Department":        u.get("department", ""),
                "JobTitle":          u.get("jobTitle", ""),
                "AccountEnabled":    u.get("accountEnabled", False),
                "CreatedDateTime":   u.get("createdDateTime"),
                "LastSignInDateTime": u.get("signInActivity", {}).get("lastSignInDateTime"),
                "LicenseStatus":     "Licensed" if u.get("assignedLicenses") else "Unlicensed",
            },
        )

    def _map_device(self, d: dict) -> ConnectorResult:
        return ConnectorResult(
            unique_key=d.get("id"),
            fields={
                "DisplayName":             d.get("displayName", ""),
                "OperatingSystem":         d.get("operatingSystem", ""),
                "OperatingSystemVersion":  d.get("operatingSystemVersion", ""),
                "TrustType":               d.get("trustType", ""),
                "IsCompliant":             d.get("isCompliant", False),
                "IsManaged":               d.get("isManaged", False),
                "RegisteredDateTime":      d.get("registeredDateTime"),
                "ApproximateLastSignInDateTime": d.get("approximateLastSignInDateTime"),
            },
        )

    def _map_group(self, g: dict) -> ConnectorResult:
        return ConnectorResult(
            unique_key=g.get("id"),
            fields={
                "DisplayName":     g.get("displayName", ""),
                "Description":     g.get("description", ""),
                "GroupTypes":      ",".join(g.get("groupTypes", [])),
                "Mail":            g.get("mail", ""),
                "MemberCount":     0,  # Requires separate Graph call; populate in extended version
                "CreatedDateTime": g.get("createdDateTime"),
            },
        )

    def _map_signin(self, s: dict) -> ConnectorResult:
        status = s.get("status", {})
        return ConnectorResult(
            unique_key=s.get("id"),
            fields={
                "UserDisplayName":  s.get("userDisplayName", ""),
                "UserPrincipalName": s.get("userPrincipalName", ""),
                "AppDisplayName":   s.get("appDisplayName", ""),
                "IPAddress":        s.get("ipAddress", ""),
                "Status":           "Success" if status.get("errorCode") == 0 else "Failure",
                "CreatedDateTime":  s.get("createdDateTime"),
                "Location":         self._format_location(s.get("location", {})),
                "ClientAppUsed":    s.get("clientAppUsed", ""),
            },
        )

    def _map_generic(self, item: dict) -> ConnectorResult:
        import json
        return ConnectorResult(
            unique_key=item.get("id"),
            fields={"Data": json.dumps(item, default=str)},
        )

    @staticmethod
    def _format_location(loc: dict) -> str:
        parts = filter(None, [loc.get("city"), loc.get("state"), loc.get("countryOrRegion")])
        return ", ".join(parts)
