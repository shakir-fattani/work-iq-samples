"""Async client for the Work IQ Gateway REST surface.

Mirrors the wire contract exercised by dotnet/rest/Program.cs:
  POST /rest/beta/conversations                       -> {"id": ...}
  POST /rest/beta/conversations/{id}/chat             -> {"messages": [...]}
  POST /rest/beta/conversations/{id}/chatOverStream   -> SSE, cumulative text
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncIterator

import httpx

REQUEST_TIMEOUT_SECONDS = 300.0
SSE_DATA_PREFIX = "data: "


class WorkIQError(Exception):
    """Work IQ returned an error or an unparseable response."""


@dataclass(frozen=True)
class Citation:
    attribution_type: str
    attribution_source: str
    provider_display_name: str
    see_more_web_url: str


@dataclass(frozen=True)
class ChatReply:
    text: str
    citations: tuple[Citation, ...] = field(default=())


def _local_timezone() -> str:
    """Work IQ requires an IANA timezone (e.g. America/Los_Angeles)."""
    tz = datetime.now().astimezone().tzinfo
    name = getattr(tz, "key", None) or str(tz)
    # A bare UTC offset like "+05:30" is not an IANA id; fall back rather than 400.
    return name if "/" in name or name == "UTC" else "UTC"


def _chat_body(message: str) -> dict[str, Any]:
    return {
        "message": {"text": message},
        "locationHint": {"timeZone": _local_timezone()},
    }


def _parse_citations(message: dict[str, Any]) -> tuple[Citation, ...]:
    return tuple(
        Citation(
            attribution_type=a.get("attributionType", ""),
            attribution_source=a.get("attributionSource", ""),
            provider_display_name=a.get("providerDisplayName", ""),
            see_more_web_url=a.get("seeMoreWebUrl", ""),
        )
        for a in message.get("attributions", [])
    )


def _last_text_message(payload: dict[str, Any]) -> dict[str, Any] | None:
    """The assistant's reply is the last message carrying a `text` field."""
    candidates = [m for m in payload.get("messages", []) if "text" in m]
    return candidates[-1] if candidates else None


class WorkIQClient:
    """One instance per user request — it is bound to that user's token."""

    def __init__(
        self,
        access_token: str,
        base_url: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=REQUEST_TIMEOUT_SECONDS,
            transport=transport,
        )

    async def __aenter__(self) -> "WorkIQClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self._client.aclose()

    async def create_conversation(self) -> str:
        response = await self._client.post("conversations", json={})
        self._raise_for_status(response, "create conversation")

        conversation_id = response.json().get("id")
        if not conversation_id:
            raise WorkIQError("no conversation id in response")
        return conversation_id

    async def chat(self, conversation_id: str, message: str) -> ChatReply:
        response = await self._client.post(
            f"conversations/{conversation_id}/chat", json=_chat_body(message)
        )
        self._raise_for_status(response, "chat")

        reply = _last_text_message(response.json())
        if reply is None:
            raise WorkIQError("no assistant message in response")

        return ChatReply(text=reply["text"], citations=_parse_citations(reply))

    async def chat_stream(
        self, conversation_id: str, message: str
    ) -> AsyncIterator[str]:
        """Yield text deltas as they arrive.

        The gateway streams cumulative, append-only text, so each event is diffed
        against the previous one. A non-prefix update would re-emit the full text;
        that does not happen under the current append-only contract.
        """
        request = self._client.build_request(
            "POST",
            f"conversations/{conversation_id}/chatOverStream",
            json=_chat_body(message),
        )
        response = await self._client.send(request, stream=True)
        try:
            if response.status_code >= 400:
                await response.aread()
                self._raise_for_status(response, "chat stream")

            previous = ""
            async for line in response.aiter_lines():
                if not line.startswith(SSE_DATA_PREFIX):
                    continue

                event = line[len(SSE_DATA_PREFIX) :].strip()
                if not event:
                    continue

                try:
                    reply = _last_text_message(json.loads(event))
                except json.JSONDecodeError:
                    continue  # Skip malformed events rather than kill the stream.

                if reply is None:
                    continue

                text = reply.get("text", "")
                delta = text[len(previous) :] if text.startswith(previous) else text
                previous = text
                if delta:
                    yield delta
        finally:
            await response.aclose()

    @staticmethod
    def _raise_for_status(response: httpx.Response, action: str) -> None:
        if response.status_code < 400:
            return

        # request-id is what Microsoft support asks for; keep it out of client replies.
        request_id = response.headers.get("request-id", "unknown")
        raise WorkIQError(
            f"{action} failed: {response.status_code} "
            f"(request-id={request_id}) {response.text[:500]}"
        )
