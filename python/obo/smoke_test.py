"""Smoke test: fakes the Work IQ gateway with httpx.MockTransport.

Diagnostic output uses print() deliberately — this is a standalone script,
not a pytest suite, and structured logging adds no value for manual runs.
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

# Fake credentials — must be set before any app.* imports so get_settings()
# picks them up. Scoped via patch.dict so they don't leak into the process
# environment if this module is ever imported by a test runner.
_TEST_ENV = {
    "AZURE_TENANT_ID": "11111111-1111-1111-1111-111111111111",
    "AZURE_CLIENT_ID": "22222222-2222-2222-2222-222222222222",
    "API_AUDIENCE": "api://22222222-2222-2222-2222-222222222222",
    "AZURE_CLIENT_SECRET": "local-dev-secret",
}

sys.path.insert(0, str(Path(__file__).parent))

BASE = "https://workiq.test/rest/beta/"


def handler(request: "httpx.Request") -> "httpx.Response":
    import httpx

    path = request.url.path
    if path.endswith("/conversations"):
        return httpx.Response(200, json={"id": "conv-42"})
    if path.endswith("/chat"):
        return httpx.Response(
            200,
            json={
                "messages": [
                    {"text": "ignored earlier turn"},
                    {
                        "text": "Your Q3 report is ready.",
                        "attributions": [
                            {
                                "attributionType": "citation",
                                "attributionSource": "sharepoint",
                                "providerDisplayName": "Q3.docx",
                                "seeMoreWebUrl": "https://contoso.example/q3",
                            }
                        ],
                    },
                ]
            },
        )
    if path.endswith("/chatOverStream"):
        # Cumulative, append-only text, exactly as the gateway streams it.
        frames = ["Hello", "Hello there", "Hello there world"]
        body = "".join(
            f"data: {json.dumps({'messages': [{'text': f}]})}\n" for f in frames
        )
        return httpx.Response(200, text=body)
    return httpx.Response(500, json={"error": "unexpected path"})


def error_handler(request: "httpx.Request") -> "httpx.Response":
    import httpx

    return httpx.Response(403, json={"error": "no copilot license"}, headers={"request-id": "abc-123"})


async def main() -> None:
    import httpx
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.workiq import WorkIQClient, WorkIQError

    # Ensure env vars are picked up by config.
    get_settings.cache_clear()

    transport = httpx.MockTransport(handler)

    async with WorkIQClient("fake-token", BASE, transport=transport) as client:
        conv = await client.create_conversation()
        assert conv == "conv-42", conv
        print("create_conversation      ->", conv)

        reply = await client.chat(conv, "what's up")
        assert reply.text == "Your Q3 report is ready.", reply.text
        assert len(reply.citations) == 1
        assert reply.citations[0].provider_display_name == "Q3.docx"
        print("chat (last text message) ->", reply.text)
        print("citations                ->", reply.citations[0].provider_display_name)

        deltas = [d async for d in client.chat_stream(conv, "stream please")]
        assert deltas == ["Hello", " there", " world"], deltas
        assert "".join(deltas) == "Hello there world"
        print("stream deltas            ->", deltas)

    # Error path: 403 must surface request-id and not raise something unrelated.
    async with WorkIQClient("t", BASE, transport=httpx.MockTransport(error_handler)) as c:
        try:
            await c.create_conversation()
            raise AssertionError("expected WorkIQError")
        except WorkIQError as exc:
            assert "request-id=abc-123" in str(exc), exc
            print("403 error path           ->", str(exc)[:60])

    # Transport error path: network failures must surface as WorkIQError.
    def transport_error_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated DNS failure")

    async with WorkIQClient("t", BASE, transport=httpx.MockTransport(transport_error_handler)) as c:
        try:
            await c.create_conversation()
            raise AssertionError("expected WorkIQError")
        except WorkIQError as exc:
            assert "simulated DNS failure" in str(exc), exc
            print("transport error (create) ->", str(exc)[:60])

        try:
            await c.chat("conv-1", "hi")
            raise AssertionError("expected WorkIQError")
        except WorkIQError as exc:
            print("transport error (chat)   ->", str(exc)[:60])

        try:
            _ = [d async for d in c.chat_stream("conv-1", "hi")]
            raise AssertionError("expected WorkIQError")
        except WorkIQError as exc:
            print("transport error (stream) ->", str(exc)[:60])

    # Auth gate: no token and malformed token must both 401 before any Work IQ call.
    from app.main import MAX_REQUEST_BODY_BYTES, app

    with TestClient(app) as tc:
        # -- /healthz (unauthenticated liveness probe) --
        r = tc.get("/healthz")
        assert r.status_code == 200, r.status_code
        assert r.json() == {"status": "ok"}
        print("healthz                  ->", r.status_code)

        # -- Security response headers --
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["cache-control"] == "no-store"
        assert r.headers["referrer-policy"] == "no-referrer"
        print("security headers         -> ok")

        # -- Body size limit (413) --
        oversized = b"x" * (MAX_REQUEST_BODY_BYTES + 1)
        r = tc.post(
            "/api/chat",
            content=oversized,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(oversized)),
            },
        )
        assert r.status_code == 413, r.status_code
        print("body size limit (CL)     ->", r.status_code)

        # -- Body size limit without Content-Length (chunked / slow path) --
        r = tc.post(
            "/api/chat",
            content=oversized,
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 413, r.status_code
        print("body size limit (chunked)->", r.status_code)

        # -- Auth gate --
        r = tc.post("/api/chat", json={"message": "hi"})
        assert r.status_code == 401, r.status_code
        print("no bearer token          ->", r.status_code, r.json()["detail"])

        r = tc.post(
            "/api/chat",
            json={"message": "hi"},
            headers={"Authorization": "Bearer not-a-jwt"},
        )
        assert r.status_code == 401, r.status_code
        print("malformed token          ->", r.status_code, r.json()["detail"])

        # Auth runs before body validation, so a bad token wins over a bad body.
        # That ordering is deliberate: unauthenticated callers learn nothing
        # about the request schema.
        r = tc.post("/api/chat", json={"message": ""}, headers={"Authorization": "Bearer x"})
        assert r.status_code == 401, r.status_code
        print("auth precedes validation ->", r.status_code)

    # Config: EXTRA_WORKIQ_HOSTS entries with trailing slashes must match
    # after _validated_workiq_host normalizes WORKIQ_HOST.
    from app.config import _allowed_hosts, _validated_workiq_host, get_settings

    get_settings.cache_clear()
    with patch.dict(os.environ, {**_TEST_ENV, "EXTRA_WORKIQ_HOSTS": "https://workiq.test/"}):
        get_settings.cache_clear()
        hosts = _allowed_hosts()
        assert "https://workiq.test" in hosts, f"trailing slash not normalized: {hosts}"
        result = _validated_workiq_host("https://workiq.test/")
        assert result == "https://workiq.test", result
        print("extra host trailing slash -> ok")
    get_settings.cache_clear()

    print("\nAll checks passed.")


if __name__ == "__main__":
    with patch.dict(os.environ, _TEST_ENV):
        asyncio.run(main())
