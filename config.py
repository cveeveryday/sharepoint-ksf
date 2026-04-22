"""
Core configuration module.
Supports both AWS (SSM/Secrets Manager) and Azure (Key Vault / App Config) environments.
"""

import os
import json
import logging
from typing import Any, Optional
from enum import Enum

logger = logging.getLogger(__name__)


class CloudProvider(str, Enum):
    AWS = "aws"
    AZURE = "azure"
    LOCAL = "local"  # for local dev / testing


def detect_provider() -> CloudProvider:
    """Auto-detect cloud provider from environment variables."""
    if os.getenv("AWS_LAMBDA_FUNCTION_NAME") or os.getenv("AWS_EXECUTION_ENV"):
        return CloudProvider.AWS
    if os.getenv("AZURE_FUNCTIONS_ENVIRONMENT") or os.getenv("WEBSITE_SITE_NAME"):
        return CloudProvider.AZURE
    return CloudProvider.LOCAL


class Config:
    """
    Unified config loader.
    - AWS: reads from SSM Parameter Store or Secrets Manager
    - Azure: reads from Key Vault or Application Settings (env vars)
    - Local: reads from environment variables or a .env file
    """

    def __init__(self, provider: Optional[CloudProvider] = None):
        self.provider = provider or detect_provider()
        self._cache: dict[str, Any] = {}
        logger.info(f"Config initialized for provider: {self.provider}")

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def get(self, key: str, default: Any = None) -> Any:
        if key in self._cache:
            return self._cache[key]

        value = self._fetch(key)
        if value is None:
            return default

        self._cache[key] = value
        return value

    def require(self, key: str) -> Any:
        value = self.get(key)
        if value is None:
            raise EnvironmentError(
                f"Required config key '{key}' not found in {self.provider} environment."
            )
        return value

    # ------------------------------------------------------------------ #
    # Internal fetchers                                                    #
    # ------------------------------------------------------------------ #

    def _fetch(self, key: str) -> Optional[Any]:
        # Always try env vars first (works everywhere, including local)
        env_value = os.getenv(key)
        if env_value is not None:
            return self._try_parse_json(env_value)

        if self.provider == CloudProvider.AWS:
            return self._fetch_aws(key)
        if self.provider == CloudProvider.AZURE:
            return self._fetch_azure(key)

        return None

    def _fetch_aws(self, key: str) -> Optional[Any]:
        """Fetch from AWS SSM Parameter Store or Secrets Manager."""
        try:
            import boto3

            # Try Secrets Manager first (for sensitive values)
            sm = boto3.client("secretsmanager")
            secret_name = os.getenv("SECRET_PREFIX", "/sharepoint-kpi") + f"/{key}"
            try:
                resp = sm.get_secret_value(SecretId=secret_name)
                raw = resp.get("SecretString") or resp.get("SecretBinary", b"").decode()
                return self._try_parse_json(raw)
            except sm.exceptions.ResourceNotFoundException:
                pass

            # Fall back to SSM Parameter Store
            ssm = boto3.client("ssm")
            param_name = os.getenv("PARAM_PREFIX", "/sharepoint-kpi") + f"/{key}"
            resp = ssm.get_parameter(Name=param_name, WithDecryption=True)
            return self._try_parse_json(resp["Parameter"]["Value"])

        except Exception as exc:
            logger.debug(f"AWS config fetch failed for '{key}': {exc}")
            return None

    def _fetch_azure(self, key: str) -> Optional[Any]:
        """Fetch from Azure Key Vault."""
        try:
            from azure.identity import DefaultAzureCredential
            from azure.keyvault.secrets import SecretClient

            vault_url = os.getenv("AZURE_KEY_VAULT_URL")
            if not vault_url:
                return None

            credential = DefaultAzureCredential()
            client = SecretClient(vault_url=vault_url, credential=credential)
            # Key Vault names must be alphanumeric + dashes
            secret_name = key.replace("_", "-").lower()
            secret = client.get_secret(secret_name)
            return self._try_parse_json(secret.value)

        except Exception as exc:
            logger.debug(f"Azure Key Vault fetch failed for '{key}': {exc}")
            return None

    @staticmethod
    def _try_parse_json(value: str) -> Any:
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
