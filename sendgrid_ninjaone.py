"""
SendGrid connector – email statistics.
NinjaOne connector  – RMM device / alert inventory.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import requests

from core.base_connector import BaseConnector, ConnectorResult, ConnectorSchema

logger = logging.getLogger(__name__)


# ======================================================================= #
# SendGrid                                                                 #
# ======================================================================= #

class SendGridConnector(BaseConnector):
    """
    Config keys:
        api_key       - SendGrid API key (needs Stats Read permission)
        resource      - One of: "stats", "category_stats", "subuser_stats"
        days_back     - How many calendar days to retrieve (default: 7)
        categories    - (optional) list of category names for category_stats
        list_name     - Target SharePoint list name
    """

    RESOURCE_SCHEMAS: dict[str, dict[str, str]] = {
        "stats": {
            "UniqueKey":        "Text",
            "Date":             "DateTime",
            "Requests":         "Number",
            "Delivered":        "Number",
            "Bounces":          "Number",
            "BounceDrops":      "Number",
            "Opens":            "Number",
            "UniqueOpens":      "Number",
            "Clicks":           "Number",
            "UniqueClicks":     "Number",
            "SpamReports":      "Number",
            "Unsubscribes":     "Number",
            "DeliveryRate":     "Number",
            "OpenRate":         "Number",
            "ClickRate":        "Number",
        },
        "category_stats": {
            "UniqueKey":    "Text",
            "Date":         "DateTime",
            "Category":     "Text",
            "Requests":     "Number",
            "Delivered":    "Number",
            "Opens":        "Number",
            "Clicks":       "Number",
            "Bounces":      "Number",
            "SpamReports":  "Number",
        },
    }

    BASE = "https://api.sendgrid.com/v3"

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.resource = config.get("resource", "stats")
        self.days_back = int(config.get("days_back", 7))
        self.categories = config.get("categories", [])
        self.list_name = config.get("list_name", f"SendGrid_{self.resource.replace('_', '').capitalize()}")

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.config['api_key']}"}

    def schema(self) -> ConnectorSchema:
        fields = self.RESOURCE_SCHEMAS.get(self.resource, {"UniqueKey": "Text", "Data": "Note"})
        return ConnectorSchema(list_name=self.list_name, fields=fields)

    def fetch(self) -> Iterator[ConnectorResult]:
        end_date = datetime.now(timezone.utc).date()
        start_date = end_date - timedelta(days=self.days_back)

        if self.resource == "stats":
            yield from self._fetch_global_stats(str(start_date), str(end_date))
        elif self.resource == "category_stats":
            yield from self._fetch_category_stats(str(start_date), str(end_date))
        elif self.resource == "subuser_stats":
            yield from self._fetch_subuser_stats(str(start_date), str(end_date))

    def _fetch_global_stats(self, start: str, end: str) -> Iterator[ConnectorResult]:
        resp = requests.get(
            f"{self.BASE}/stats",
            headers=self._headers(),
            params={"start_date": start, "end_date": end, "aggregated_by": "day"},
            timeout=30,
        )
        resp.raise_for_status()
        for entry in resp.json():
            m = entry.get("stats", [{}])[0].get("metrics", {})
            date_str = entry.get("date", "")
            delivered = m.get("delivered", 0)
            requests_count = m.get("requests", 0)
            opens = m.get("opens", 0)
            clicks = m.get("clicks", 0)
            yield ConnectorResult(
                unique_key=f"global_{date_str}",
                fields={
                    "Date":          date_str,
                    "Requests":      requests_count,
                    "Delivered":     delivered,
                    "Bounces":       m.get("bounces", 0),
                    "BounceDrops":   m.get("bounce_drops", 0),
                    "Opens":         opens,
                    "UniqueOpens":   m.get("unique_opens", 0),
                    "Clicks":        clicks,
                    "UniqueClicks":  m.get("unique_clicks", 0),
                    "SpamReports":   m.get("spam_reports", 0),
                    "Unsubscribes":  m.get("unsubscribes", 0),
                    "DeliveryRate":  round(delivered / requests_count * 100, 2) if requests_count else 0,
                    "OpenRate":      round(opens / delivered * 100, 2) if delivered else 0,
                    "ClickRate":     round(clicks / opens * 100, 2) if opens else 0,
                },
            )

    def _fetch_category_stats(self, start: str, end: str) -> Iterator[ConnectorResult]:
        categories = self.categories or [""]
        for category in categories:
            params: dict = {"start_date": start, "end_date": end, "aggregated_by": "day"}
            if category:
                params["categories"] = category
            resp = requests.get(
                f"{self.BASE}/categories/stats",
                headers=self._headers(),
                params=params,
                timeout=30,
            )
            resp.raise_for_status()
            for entry in resp.json():
                date_str = entry.get("date", "")
                for stat in entry.get("stats", []):
                    m = stat.get("metrics", {})
                    cat_name = stat.get("name", category)
                    yield ConnectorResult(
                        unique_key=f"{cat_name}_{date_str}",
                        fields={
                            "Date":        date_str,
                            "Category":    cat_name,
                            "Requests":    m.get("requests", 0),
                            "Delivered":   m.get("delivered", 0),
                            "Opens":       m.get("opens", 0),
                            "Clicks":      m.get("clicks", 0),
                            "Bounces":     m.get("bounces", 0),
                            "SpamReports": m.get("spam_reports", 0),
                        },
                    )

    def _fetch_subuser_stats(self, start: str, end: str) -> Iterator[ConnectorResult]:
        resp = requests.get(
            f"{self.BASE}/subusers/stats",
            headers=self._headers(),
            params={"start_date": start, "end_date": end, "aggregated_by": "day"},
            timeout=30,
        )
        resp.raise_for_status()
        for entry in resp.json():
            date_str = entry.get("date", "")
            for stat in entry.get("stats", []):
                m = stat.get("metrics", {})
                name = stat.get("name", "")
                yield ConnectorResult(
                    unique_key=f"{name}_{date_str}",
                    fields={
                        "Date":      date_str,
                        "Category":  name,
                        "Requests":  m.get("requests", 0),
                        "Delivered": m.get("delivered", 0),
                        "Opens":     m.get("opens", 0),
                        "Clicks":    m.get("clicks", 0),
                        "Bounces":   m.get("bounces", 0),
                    },
                )


# ======================================================================= #
# NinjaOne (RMM)                                                           #
# ======================================================================= #

class NinjaOneConnector(BaseConnector):
    """
    Config keys:
        client_id     - NinjaOne OAuth client ID
        client_secret - NinjaOne OAuth client secret
        instance_url  - e.g. https://app.ninjarmm.com  (no trailing slash)
        resource      - One of: "devices", "alerts", "activities", "organizations"
        list_name     - Target SharePoint list name
    """

    RESOURCE_SCHEMAS: dict[str, dict[str, str]] = {
        "devices": {
            "UniqueKey":         "Text",
            "SystemName":        "Text",
            "DNSName":           "Text",
            "OrganizationName":  "Text",
            "OS":                "Text",
            "NodeClass":         "Text",
            "Online":            "Boolean",
            "LastContact":       "DateTime",
            "IPAddress":         "Text",
            "MacAddress":        "Text",
            "Manufacturer":      "Text",
            "Model":             "Text",
            "SerialNumber":      "Text",
            "CPUModel":          "Text",
            "TotalRamMB":        "Number",
            "DiskSpaceGB":       "Number",
        },
        "alerts": {
            "UniqueKey":       "Text",
            "DeviceName":      "Text",
            "Severity":        "Text",
            "Message":         "Note",
            "Source":          "Text",
            "Type":            "Text",
            "Status":          "Text",
            "CreatedAt":       "DateTime",
            "ResolvedAt":      "DateTime",
        },
        "organizations": {
            "UniqueKey":   "Text",
            "Name":        "Text",
            "Description": "Text",
            "NodeCount":   "Number",
        },
    }

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.resource = config.get("resource", "devices")
        self.instance_url = config.get("instance_url", "https://app.ninjarmm.com").rstrip("/")
        self.list_name = config.get("list_name", f"NinjaOne_{self.resource.capitalize()}")
        self._token: str | None = None

    def schema(self) -> ConnectorSchema:
        fields = self.RESOURCE_SCHEMAS.get(self.resource, {"UniqueKey": "Text", "Data": "Note"})
        return ConnectorSchema(list_name=self.list_name, fields=fields)

    def authenticate(self) -> None:
        resp = requests.post(
            f"{self.instance_url}/ws/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.config["client_id"],
                "client_secret": self.config["client_secret"],
                "scope": "monitoring management",
            },
            timeout=30,
        )
        resp.raise_for_status()
        self._token = resp.json()["access_token"]
        logger.info("NinjaOne authenticated.")

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"}

    def fetch(self) -> Iterator[ConnectorResult]:
        if not self._token:
            self.authenticate()

        mapper = {
            "devices":       self._fetch_devices,
            "alerts":        self._fetch_alerts,
            "organizations": self._fetch_organizations,
        }.get(self.resource, self._fetch_generic)
        yield from mapper()

    def _fetch_devices(self) -> Iterator[ConnectorResult]:
        resp = requests.get(
            f"{self.instance_url}/v2/devices-detailed",
            headers=self._headers(),
            timeout=60,
        )
        resp.raise_for_status()
        for d in resp.json():
            system = d.get("system", {})
            disks = d.get("volumes", [])
            total_disk = sum(v.get("size", 0) for v in disks) / (1024 ** 3)  # bytes → GB
            yield ConnectorResult(
                unique_key=str(d.get("id")),
                fields={
                    "SystemName":       d.get("systemName", ""),
                    "DNSName":          d.get("dnsName", ""),
                    "OrganizationName": d.get("organizationName", ""),
                    "OS":               system.get("operatingSystem", ""),
                    "NodeClass":        d.get("nodeClass", ""),
                    "Online":           d.get("online", False),
                    "LastContact":      d.get("lastContact"),
                    "IPAddress":        d.get("ipAddresses", [""])[0] if d.get("ipAddresses") else "",
                    "MacAddress":       system.get("macAddress", ""),
                    "Manufacturer":     system.get("manufacturer", ""),
                    "Model":            system.get("model", ""),
                    "SerialNumber":     system.get("serialNumber", ""),
                    "CPUModel":         (d.get("processors") or [{}])[0].get("name", ""),
                    "TotalRamMB":       round((system.get("totalPhysicalMemory", 0)) / (1024 ** 2)),
                    "DiskSpaceGB":      round(total_disk, 1),
                },
            )

    def _fetch_alerts(self) -> Iterator[ConnectorResult]:
        resp = requests.get(
            f"{self.instance_url}/v2/alerts",
            headers=self._headers(),
            timeout=60,
        )
        resp.raise_for_status()
        for a in resp.json():
            yield ConnectorResult(
                unique_key=str(a.get("uid") or a.get("id")),
                fields={
                    "DeviceName": a.get("deviceName", ""),
                    "Severity":   a.get("severity", ""),
                    "Message":    a.get("message", ""),
                    "Source":     a.get("source", ""),
                    "Type":       a.get("type", ""),
                    "Status":     a.get("status", "OPEN"),
                    "CreatedAt":  a.get("createTime"),
                    "ResolvedAt": a.get("resolveTime"),
                },
            )

    def _fetch_organizations(self) -> Iterator[ConnectorResult]:
        resp = requests.get(
            f"{self.instance_url}/v2/organizations",
            headers=self._headers(),
            timeout=60,
        )
        resp.raise_for_status()
        for org in resp.json():
            yield ConnectorResult(
                unique_key=str(org.get("id")),
                fields={
                    "Name":        org.get("name", ""),
                    "Description": org.get("description", ""),
                    "NodeCount":   org.get("nodeCount", 0),
                },
            )

    def _fetch_generic(self) -> Iterator[ConnectorResult]:
        import json
        resp = requests.get(
            f"{self.instance_url}/v2/{self.resource}",
            headers=self._headers(),
            timeout=60,
        )
        resp.raise_for_status()
        for item in resp.json():
            yield ConnectorResult(
                unique_key=str(item.get("id", "")),
                fields={"Data": json.dumps(item, default=str)},
            )
