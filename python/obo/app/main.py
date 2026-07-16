"""FastAPI backend that brokers Work IQ calls on behalf of the signed-in user.

Flow per request:
  frontend token -> validate -> OBO exchange -> Work IQ token -> call gateway
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Annotated, AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .auth import InvalidToken, TokenValidator, WorkIQTokenExchange
from .config import Settings, get_settings
from .workiq import WorkIQClient, WorkIQError

logger = logging.getLogger(__name__)

BEARER_SCHEME = "bearer"


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    conversation_id: str | None = None


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
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return parts[1]


async def workiq_token(
    inbound_token: Annotated[str, Depends(_bearer_token)],
) -> str:
    """Validate the caller's token and exchange it for a Work IQ token."""
    validator: TokenValidator = app.state.validator
    exchange: WorkIQTokenExchange = app.state.exchange

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
    except Exception as exc:
        # A failure here is usually consent, licensing, or a misconfigured app
        # registration — log it with the user for triage, but do not leak it.
        logger.error("OBO exchange failed for %s: %s", claims.get("oid"), exc)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Unable to obtain Work IQ access for this user",
        ) from exc


def _client(token: str, settings: Settings) -> WorkIQClient:
    return WorkIQClient(access_token=token, base_url=settings.workiq_base)


@app.post("/api/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    token: Annotated[str, Depends(workiq_token)],
) -> ChatResponse:
    settings: Settings = app.state.settings

    try:
        async with _client(token, settings) as client:
            conversation_id = request.conversation_id or await client.create_conversation()
            reply = await client.chat(conversation_id, request.message)
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
) -> StreamingResponse:
    settings: Settings = app.state.settings

    async def events() -> AsyncIterator[str]:
        try:
            async with _client(token, settings) as client:
                conversation_id = (
                    request.conversation_id or await client.create_conversation()
                )
                yield f"event: conversation\ndata: {conversation_id}\n\n"

                # JSON-encode each delta: raw newlines in the text would
                # otherwise terminate the SSE frame early.
                async for delta in client.chat_stream(conversation_id, request.message):
                    yield f"data: {json.dumps({'text': delta})}\n\n"
        except WorkIQError as exc:
            # The response has already started, so the error rides the stream.
            logger.error("work iq stream failed: %s", exc)
            yield "event: error\ndata: Work IQ request failed\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")
