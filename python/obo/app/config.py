"""Configuration for the Work IQ OBO backend, loaded from the environment.

Missing required values fail at startup (when ``get_settings()`` is first called)
rather than on the first request.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

# The Work IQ Gateway multiplexes REST/A2A/MCP on one host behind a path prefix
# (/rest, /a2a, /tools). Only the REST surface is used here.
WORKIQ_DEFAULT_HOST = "https://workiq.svc.cloud.microsoft"
WORKIQ_PATH = "/rest/beta"

# Work IQ resource. WorkIQAgent.Ask is granted by admin consent on the app
# registration, so `.default` resolves to it without naming the scope here.
WORKIQ_SCOPE = "api://workiq.svc.cloud.microsoft/.default"

# Scope the frontend must present on its token to this backend.
DEFAULT_REQUIRED_SCOPE = "access_as_user"

ENTRA_AUTHORITY = "https://login.microsoftonline.com"

# Audience used when exchanging a managed identity token for a client assertion
# (workload identity federation). Only used in the secretless configuration.
TOKEN_EXCHANGE_SCOPE = "api://AzureADTokenExchange/.default"


class ConfigError(RuntimeError):
    """A required environment variable is missing or malformed."""


@dataclass(frozen=True)
class Settings:
    """Immutable runtime configuration."""

    tenant_id: str
    client_id: str
    api_audience: str
    required_scope: str
    workiq_host: str
    client_secret: str | None

    @property
    def issuer(self) -> str:
        return f"{ENTRA_AUTHORITY}/{self.tenant_id}/v2.0"

    @property
    def jwks_uri(self) -> str:
        return f"{ENTRA_AUTHORITY}/{self.tenant_id}/discovery/v2.0/keys"

    @property
    def workiq_base(self) -> str:
        return f"{self.workiq_host.rstrip('/')}{WORKIQ_PATH}/"

    @property
    def uses_managed_identity(self) -> bool:
        """True when no secret is configured, so a federated assertion is used."""
        return self.client_secret is None


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} must be set")
    return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    secret = os.environ.get("AZURE_CLIENT_SECRET", "").strip()
    return Settings(
        tenant_id=_require("AZURE_TENANT_ID"),
        client_id=_require("AZURE_CLIENT_ID"),
        api_audience=_require("API_AUDIENCE"),
        required_scope=os.environ.get("REQUIRED_SCOPE", DEFAULT_REQUIRED_SCOPE).strip(),
        workiq_host=os.environ.get("WORKIQ_HOST", WORKIQ_DEFAULT_HOST).strip(),
        client_secret=secret or None,
    )
