"""
Cisco Meraki connector.
Fetches network devices, clients, or organization inventory via Meraki Dashboard API.
"""

import logging
from typing import Any, Iterator

import requests

from base_connector import BaseConnector, ConnectorResult, ConnectorSchema

logger = logging.getLogger(__name__)

MERAKI_BASE = "https://api.meraki.com/api/v1"


class MerakiConnector(BaseConnector):
    """
    Config keys:
        api_key       - Meraki Dashboard API key
        org_id        - Meraki organization ID
        resource      - One of: "devices", "clients", "networks", "inventory"
        network_id    - (optional) scope to specific network
        list_name     - Target SharePoint list name
    """

    RESOURCE_SCHEMAS: dict[str, dict[str, str]] = {
        "devices": {
            "UniqueKey":   "Text",
            "Name":        "Text",
            "Model":       "Text",
            "Serial":      "Text",
            "Mac":         "Text",
            "NetworkId":   "Text",
            "NetworkName": "Text",
            "Status":      "Text",
            "Firmware":    "Text",
            "IPAddress":   "Text",
            "Tags":        "Text",
            "LastReportedAt": "DateTime",
        },
        "clients": {
            "UniqueKey":      "Text",
            "Hostname":       "Text",
            "Description":    "Text",
            "Mac":            "Text",
            "IP":             "Text",
            "Manufacturer":   "Text",
            "OS":             "Text",
            "VLAN":           "Text",
            "Status":         "Text",
            "Usage":          "Number",
            "FirstSeen":      "DateTime",
            "LastSeen":       "DateTime",
        },
        "inventory": {
            "UniqueKey":     "Text",
            "Serial":        "Text",
            "Model":         "Text",
            "Mac":           "Text",
            "NetworkId":     "Text",
            "OrderNumber":   "Text",
            "ClaimedAt":     "DateTime",
            "LicenseExpirationDate": "DateTime",
        },
        "networks": {
            "UniqueKey":      "Text",
            "Name":           "Text",
            "ProductTypes":   "Text",
            "TimeZone":       "Text",
            "Tags":           "Text",
            "EnrollmentString": "Text",
        },
    }

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.resource = config.get("resource", "devices")
        self.org_id = config["org_id"]
        self.network_id = config.get("network_id")
        self.list_name = config.get("list_name", f"Meraki_{self.resource.capitalize()}")

    def _headers(self) -> dict:
        return {
            "X-Cisco-Meraki-API-Key": self.config["api_key"],
            "Content-Type": "application/json",
        }

    def schema(self) -> ConnectorSchema:
        fields = self.RESOURCE_SCHEMAS.get(self.resource, {"UniqueKey": "Text", "Data": "Note"})
        return ConnectorSchema(list_name=self.list_name, fields=fields)

    def fetch(self) -> Iterator[ConnectorResult]:
        mapper = {
            "devices":   self._fetch_devices,
            "clients":   self._fetch_clients,
            "inventory": self._fetch_inventory,
            "networks":  self._fetch_networks,
        }.get(self.resource, self._fetch_generic)
        yield from mapper()

    # ------------------------------------------------------------------ #
    # Resource fetchers                                                    #
    # ------------------------------------------------------------------ #

    def _fetch_devices(self) -> Iterator[ConnectorResult]:
        # Get all networks to enrich device data
        networks = self._get_all_networks()
        net_map = {n["id"]: n["name"] for n in networks}

        url = f"{MERAKI_BASE}/organizations/{self.org_id}/devices/statuses"
        resp = requests.get(url, headers=self._headers(), timeout=60)
        resp.raise_for_status()

        for device in resp.json():
            yield ConnectorResult(
                unique_key=device.get("serial"),
                fields={
                    "Name":        device.get("name", ""),
                    "Model":       device.get("model", ""),
                    "Serial":      device.get("serial", ""),
                    "Mac":         device.get("mac", ""),
                    "NetworkId":   device.get("networkId", ""),
                    "NetworkName": net_map.get(device.get("networkId", ""), ""),
                    "Status":      device.get("status", ""),
                    "Firmware":    device.get("firmware", ""),
                    "IPAddress":   device.get("lanIp") or device.get("publicIp", ""),
                    "Tags":        " ".join(device.get("tags", [])),
                    "LastReportedAt": device.get("lastReportedAt"),
                },
            )

    def _fetch_clients(self) -> Iterator[ConnectorResult]:
        networks = [self.network_id] if self.network_id else [n["id"] for n in self._get_all_networks()]
        for net_id in networks:
            url = f"{MERAKI_BASE}/networks/{net_id}/clients"
            params = {"timespan": 86400, "perPage": 1000}
            resp = requests.get(url, headers=self._headers(), params=params, timeout=60)
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            for client in resp.json():
                usage = client.get("usage", {})
                yield ConnectorResult(
                    unique_key=client.get("id"),
                    fields={
                        "Hostname":     client.get("hostname", ""),
                        "Description":  client.get("description", ""),
                        "Mac":          client.get("mac", ""),
                        "IP":           client.get("ip", ""),
                        "Manufacturer": client.get("manufacturer", ""),
                        "OS":           client.get("os", ""),
                        "VLAN":         str(client.get("vlan", "")),
                        "Status":       client.get("status", ""),
                        "Usage":        (usage.get("sent", 0) + usage.get("recv", 0)) / 1024,  # MB
                        "FirstSeen":    client.get("firstSeen"),
                        "LastSeen":     client.get("lastSeen"),
                    },
                )

    def _fetch_inventory(self) -> Iterator[ConnectorResult]:
        url = f"{MERAKI_BASE}/organizations/{self.org_id}/inventory/devices"
        resp = requests.get(url, headers=self._headers(), timeout=60)
        resp.raise_for_status()
        for item in resp.json():
            yield ConnectorResult(
                unique_key=item.get("serial"),
                fields={
                    "Serial":       item.get("serial", ""),
                    "Model":        item.get("model", ""),
                    "Mac":          item.get("mac", ""),
                    "NetworkId":    item.get("networkId", ""),
                    "OrderNumber":  item.get("orderNumber", ""),
                    "ClaimedAt":    item.get("claimedAt"),
                    "LicenseExpirationDate": item.get("licenseExpirationDate"),
                },
            )

    def _fetch_networks(self) -> Iterator[ConnectorResult]:
        for net in self._get_all_networks():
            yield ConnectorResult(
                unique_key=net.get("id"),
                fields={
                    "Name":             net.get("name", ""),
                    "ProductTypes":     ",".join(net.get("productTypes", [])),
                    "TimeZone":         net.get("timeZone", ""),
                    "Tags":             " ".join(net.get("tags", [])),
                    "EnrollmentString": net.get("enrollmentString", ""),
                },
            )

    def _fetch_generic(self) -> Iterator[ConnectorResult]:
        import json
        url = f"{MERAKI_BASE}/organizations/{self.org_id}/{self.resource}"
        resp = requests.get(url, headers=self._headers(), timeout=60)
        resp.raise_for_status()
        for item in resp.json():
            yield ConnectorResult(
                unique_key=item.get("id") or item.get("serial"),
                fields={"Data": json.dumps(item, default=str)},
            )

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    def _get_all_networks(self) -> list[dict]:
        url = f"{MERAKI_BASE}/organizations/{self.org_id}/networks"
        resp = requests.get(url, headers=self._headers(), timeout=60)
        resp.raise_for_status()
        return resp.json()
