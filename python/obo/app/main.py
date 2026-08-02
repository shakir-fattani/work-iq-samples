"""FastAPI backend that brokers Work IQ calls on behalf of the signed-in user.

Flow per request:
  frontend token -> validate -> OBO exchange -> Work IQ token -> call gateway
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated, AsyncIterator

from azure.core.exceptions import ClientAuthenticationError, HttpResponseError
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from .auth import InvalidToken, TokenValidator, WorkIQTokenExchange
from .config import Settings, get_settings
from .workiq import CONV_ID_PATTERN, WorkIQClient, WorkIQError

logger = logging.getLogger(__name__)

BEARER_SCHEME = "bearer"
MAX_REQUEST_BODY_BYTES = 64 * 1024  # 64 KB — well above the 8 KB message limit


class _BodySizeLimitMiddleware:
    """Reject requests whose Content-Length exceeds the configured cap."""

    def __init__(self, app: ASGIApp, *, max_bytes: int = MAX_REQUEST_BODY_BYTES) -> None:
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            length = headers.get(b"content-length")
            if length is not None and int(length) > self._max_bytes:
                response = JSONResponse(
                    {"detail": "Request body too large"},
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                )
                await response(scope, receive, send)
                return
        await self._app(scope, receive, send)


class _SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add baseline security headers to every response."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    conversation_id: str | None = Field(
        default=None, pattern=CONV_ID_PATTERN, max_length=128
    )
    time_zone: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9_+\-/]+$", max_length=64
    )


class CitationModel(BaseModel):
    type: str
    source: str
    provider: str
    url: str


class ChatResponse(BaseModel):
    conversation_id: str
    text: str
    citations: list[CitationModel]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.settings = settings
    app.state.validator = TokenValidator(settings)
    app.state.exchange = WorkIQTokenExchange(settings)
    logger.info(
        "ready — auth=%s, workiq=%s",
        "managed-identity" if settings.uses_managed_identity else "client-secret",
        settings.workiq_base,
    )
    try:
        yield
    finally:
        await app.state.exchange.close()


app = FastAPI(title="Work IQ OBO Backend", lifespan=lifespan)
app.add_middleware(_SecurityHeadersMiddleware)
app.add_middleware(_BodySizeLimitMiddleware)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


def _bearer_token(authorization: Annotated[str | None, Header()] = None) -> str:
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != BEARER_SCHEME:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header must use Bearer scheme",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return parts[1].strip()


async def workiq_token(
    request: Request,
    inbound_token: Annotated[str, Depends(_bearer_token)],
) -> str:
    """Validate the caller's token and exchange it for a Work IQ token."""
    validator: TokenValidator = request.app.state.validator
    exchange: WorkIQTokenExchange = request.app.state.exchange

    try:
        claims = await validator.validate(inbound_token)
    except InvalidToken as exc:
        logger.warning("rejected inbound token: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    try:
        return await exchange.token_for(inbound_token)
    except (ClientAuthenticationError, HttpResponseError) as exc:
        # Consent, licensing, or misconfigured app registration — log for triage.
        logger.error("OBO exchange failed for %s: %s", claims.get("oid"), exc)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Unable to obtain Work IQ access for this user",
        ) from exc


def _get_settings(request: Request) -> Settings:
    return request.app.state.settings


def _client(token: str, settings: Settings) -> WorkIQClient:
    return WorkIQClient(access_token=token, base_url=settings.workiq_base)


@app.post("/api/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    token: Annotated[str, Depends(workiq_token)],
    settings: Annotated[Settings, Depends(_get_settings)],
) -> ChatResponse:
    try:
        async with _client(token, settings) as client:
            conversation_id = request.conversation_id or await client.create_conversation()
            reply = await client.chat(
                conversation_id, request.message, time_zone=request.time_zone
            )
    except WorkIQError as exc:
        logger.error("work iq call failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Work IQ request failed"
        ) from exc

    return ChatResponse(
        conversation_id=conversation_id,
        text=reply.text,
        citations=[
            CitationModel(
                type=c.attribution_type,
                source=c.attribution_source,
                provider=c.provider_display_name,
                url=c.see_more_web_url,
            )
            for c in reply.citations
        ],
    )


@app.post("/api/chat/stream")
async def chat_stream(
    request: ChatRequest,
    token: Annotated[str, Depends(workiq_token)],
    settings: Annotated[Settings, Depends(_get_settings)],
) -> StreamingResponse:

    async def events() -> AsyncGenerator[str, None]:
        try:
            async with _client(token, settings) as client:
                conversation_id = (
                    request.conversation_id or await client.create_conversation()
                )
                yield f"event: conversation\ndata: {json.dumps({'conversation_id': conversation_id})}\n\n"

                # JSON-encode each delta: raw newlines in the text would
                # otherwise terminate the SSE frame early.
                async for delta in client.chat_stream(
                    conversation_id, request.message, time_zone=request.time_zone
                ):
                    yield f"data: {json.dumps({'text': delta})}\n\n"
            yield "event: done\ndata: \n\n"
        except WorkIQError as exc:
            # The HTTP 200 is already committed, so the error rides the stream.
            # Clients must handle "error" events to detect mid-stream failures.
            logger.error("work iq stream failed: %s", exc)
            yield "event: error\ndata: upstream request failed\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")
