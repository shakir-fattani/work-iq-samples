"""Validate the frontend's token, then exchange it for a Work IQ token via OBO.

Two distinct tokens are involved and they are not interchangeable:

  1. Inbound  — aud = this backend's Application ID URI (API_AUDIENCE).
                Issued to the frontend. Work IQ rejects it (401, audience mismatch).
  2. Outbound — aud = api://workiq.svc.cloud.microsoft. Minted here by the
                On-Behalf-Of flow, carrying the same user identity.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

import jwt
from azure.identity import DefaultAzureCredential as SyncDefaultAzureCredential
from azure.identity.aio import OnBehalfOfCredential
from jwt import PyJWKClient

from .config import TOKEN_EXCHANGE_SCOPE, WORKIQ_SCOPE, Settings


class InvalidToken(Exception):
    """The inbound token failed signature, audience, issuer, or scope checks."""


class TokenValidator:
    """Validates inbound tokens against Entra's published signing keys.

    PyJWKClient caches keys after the first fetch, so the blocking call it makes
    is offloaded to a worker thread rather than stalling the event loop.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._jwks_client = PyJWKClient(settings.jwks_uri, lifespan=3600)

    async def validate(self, token: str) -> dict[str, Any]:
        try:
            signing_key = await asyncio.to_thread(
                self._jwks_client.get_signing_key_from_jwt, token
            )
            claims: dict[str, Any] = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self._settings.api_audience,
                issuer=self._settings.issuer,
                options={"require": ["exp", "aud", "iss"]},
            )
        except jwt.PyJWTError as exc:
            raise InvalidToken(f"token rejected: {exc}") from exc

        self._require_scope(claims)
        return claims

    def _require_scope(self, claims: dict[str, Any]) -> None:
        if "scp" not in claims:
            raise InvalidToken(
                "app-only tokens are not accepted; a delegated user token is required"
            )
        granted = set(str(claims["scp"]).split())
        if self._settings.required_scope not in granted:
            raise InvalidToken(
                f"token is missing the required scope '{self._settings.required_scope}'"
            )


def _federated_assertion(credential: SyncDefaultAzureCredential) -> Callable[[], str]:
    """Build a client assertion from a managed identity token.

    This is where DefaultAzureCredential belongs in an OBO backend: it proves the
    *app's* identity so no client secret is needed. It cannot perform the OBO
    exchange itself — that needs the user's assertion, which only OnBehalfOfCredential
    accepts. The callable is sync because azure-identity invokes it synchronously.
    """

    def assertion() -> str:
        return credential.get_token(TOKEN_EXCHANGE_SCOPE).token

    return assertion


class WorkIQTokenExchange:
    """Mints Work IQ access tokens on behalf of the calling user."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._mi_credential = (
            SyncDefaultAzureCredential() if settings.uses_managed_identity else None
        )

    async def token_for(self, user_assertion: str) -> str:
        """Exchange a validated inbound token for a Work IQ access token.

        A fresh credential per request keeps user identities isolated. The trade-off
        is a round trip to Entra on every call — add a cache keyed by a hash of the
        assertion if that latency matters.
        """
        credential = self._build_credential(user_assertion)
        async with credential:
            access_token = await credential.get_token(WORKIQ_SCOPE)
            return access_token.token

    def _build_credential(self, user_assertion: str) -> OnBehalfOfCredential:
        common = {
            "tenant_id": self._settings.tenant_id,
            "client_id": self._settings.client_id,
            "user_assertion": user_assertion,
        }

        if self._mi_credential is not None:
            return OnBehalfOfCredential(
                client_assertion_func=_federated_assertion(self._mi_credential),
                **common,
            )

        return OnBehalfOfCredential(
            client_secret=self._settings.client_secret,
            **common,
        )

    async def close(self) -> None:
        if self._mi_credential is not None:
            await asyncio.to_thread(self._mi_credential.close)
