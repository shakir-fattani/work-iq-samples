# Work IQ Python OBO Sample

A FastAPI **middle-tier** service that accepts your frontend's access token, exchanges it for a Work IQ token via the [On-Behalf-Of flow](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-on-behalf-of-flow), and calls the [Copilot Chat REST API](https://learn.microsoft.com/en-us/microsoft-365-copilot/extensibility/api/ai-services/chat/overview) through the **Work IQ Gateway**.

Every other sample in this repo is a **public client** that signs a user in directly. This one is different: the user signs in to *your* frontend, and this service brokers the Work IQ call for them.

```
Frontend  ──token(aud=your API)──▶  This service  ──token(aud=Work IQ)──▶  Gateway
                                    │
                                    └── validate ──▶ OBO exchange
```

Supports both **synchronous** and **streaming** (SSE) modes.

## API reference

| Operation | Method | Path | Docs |
|-----------|--------|------|------|
| Create conversation | `POST` | `/rest/beta/conversations` | [Docs](https://learn.microsoft.com/en-us/microsoft-365-copilot/extensibility/api/ai-services/chat/copilotroot-post-conversations) |
| Chat (sync) | `POST` | `/rest/beta/conversations/{id}/chat` | [Docs](https://learn.microsoft.com/en-us/microsoft-365-copilot/extensibility/api/ai-services/chat/copilotconversation-chat) |
| Chat (stream) | `POST` | `/rest/beta/conversations/{id}/chatOverStream` | [Docs](https://learn.microsoft.com/en-us/microsoft-365-copilot/extensibility/api/ai-services/chat/copilotconversation-chatoverstream) |

## Why you can't just forward the frontend's token

A token's audience is baked into the signed JWT. The token your frontend holds has
`aud` set to *your* API — the Gateway rejects it with `401`. You cannot re-point it at
another resource; you exchange it for a new one. That exchange is OBO.

**`DefaultAzureCredential` cannot do this exchange.** Its chain (managed identity,
environment service principal, Azure CLI) issues tokens for the *app's own* identity or
a *developer's* identity. None of them accept an inbound user token and re-issue it for
a different resource. And `WorkIQAgent.Ask` is delegated-only — an app-only token gets
you nothing, because responses depend on the signed-in user's Copilot license and their
own data access.

`azure.identity.aio.OnBehalfOfCredential` is the credential that does this.

`DefaultAzureCredential` still has a real job here: proving the *app's* identity so no
client secret is ever deployed. Leave `AZURE_CLIENT_SECRET` unset and the service uses a
managed identity federated credential as its client assertion — see [`app/auth.py`](app/auth.py):

```python
def assertion() -> str:
    return credential.get_token("api://AzureADTokenExchange/.default").token

OnBehalfOfCredential(
    tenant_id=..., client_id=...,
    client_assertion_func=assertion,   # app identity  — DefaultAzureCredential
    user_assertion=inbound_token,      # user identity — from your frontend
)
```

Both identities are required: the app proves it is allowed to ask, the user token
determines what comes back.

## Prerequisites

1. **Microsoft 365 Copilot license** on your test user.
2. **Two Entra app registrations.** `scripts/admin-setup.sh` at the repo root does **not**
   cover this sample — it creates a *public* client for the CLI samples, and OBO requires
   a *confidential* client. See [App registration](#app-registration) below.
3. **Python 3.10+**.

## App registration

**Frontend app** (SPA / public client) — requests `api://<BACKEND_APP_ID>/access_as_user`.

**Backend app** (this service — confidential client):

| Blade | Setting |
|-------|---------|
| Expose an API | Application ID URI `api://<BACKEND_APP_ID>`, scope `access_as_user` |
| API permissions | `Work IQ` → delegated `WorkIQAgent.Ask` → **Grant admin consent** |
| Certificates & secrets | In Azure, prefer a **federated credential** bound to your managed identity over a client secret |

```bash
# Ensure the Work IQ service principal exists in your tenant
az ad sp create --id fdcc1f02-fc51-4226-8753-f668596af7f7

# Grant this service the delegated Work IQ permission, then consent
az ad app permission add --id <BACKEND_APP_ID> \
  --api fdcc1f02-fc51-4226-8753-f668596af7f7 \
  --api-permissions "0b1715fd-f4bf-4c63-b16d-5be31f9847c2=Scope"
az ad app permission admin-consent --id <BACKEND_APP_ID>
```

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # fill in tenant, client id, audience
set -a && source .env && set +a

uvicorn app.main:app --reload
```

```bash
curl -X POST localhost:8000/api/chat \
  -H "Authorization: Bearer <token your frontend got for api://<BACKEND_APP_ID>/access_as_user>" \
  -H "Content-Type: application/json" \
  -d '{"message": "What meetings do I have tomorrow?"}'
```

```json
{
  "conversation_id": "conv-123",
  "text": "You have 3 meetings scheduled...",
  "citations": [
    { "type": "citation", "source": "sharepoint", "provider": "Q3.docx", "url": "https://..." }
  ]
}
```

Pass `conversation_id` back on later turns to continue the same conversation with full context.

### Verify without credentials

```bash
python smoke_test.py
```

Fakes the Gateway with `httpx.MockTransport` and checks conversation creation, citation
parsing, streaming deltas, the `403` error path, and the auth gate. No tenant, no license,
no network.

## Endpoints

| Endpoint | Mode | Response |
|----------|------|----------|
| `POST /api/chat` | Synchronous | JSON — `conversation_id`, `text`, `citations` |
| `POST /api/chat/stream` | SSE | `event: conversation` then `data: {"text": "<delta>"}` frames |

Request body for both: `{"message": "...", "conversation_id": "..."}` (`conversation_id` optional).

## Layout

| File | Purpose |
|------|---------|
| [`app/config.py`](app/config.py) | Environment config; fails fast on missing values |
| [`app/auth.py`](app/auth.py) | Inbound JWT validation + the OBO exchange |
| [`app/workiq.py`](app/workiq.py) | Async Gateway client (sync + streaming) |
| [`app/main.py`](app/main.py) | FastAPI routes |
| [`smoke_test.py`](smoke_test.py) | Gateway faked via `httpx.MockTransport` |

## How it works

```
Frontend            This service                       Gateway
  |                      |                                |
  |-- POST /api/chat --->|                                |
  |   Bearer <user tok>  |                                |
  |                      |-- validate signature/aud/scp   |
  |                      |   against Entra JWKS           |
  |                      |                                |
  |                      |-- OBO exchange ---▶ Entra      |
  |                      |◀-- token(aud=Work IQ) --       |
  |                      |                                |
  |                      |-- POST .../conversations ----->|
  |                      |<-- 201 { "id": "conv-123" } ---|
  |                      |-- POST .../conv-123/chat ----->|
  |                      |<-- 200 { "messages": [...] } --|
  |<-- 200 JSON ---------|                                |
```

Each SSE event from the Gateway contains the **full conversation state** (cumulative, not
incremental). [`app/workiq.py`](app/workiq.py) diffs against the previous event and yields
only new text, so `/api/chat/stream` emits true deltas.

Auth is resolved before body validation, so an unauthenticated caller gets `401` and
learns nothing about the request schema.

## Dependencies

| Package | Purpose |
|---------|---------|
| `azure-identity` | `OnBehalfOfCredential` for the exchange; `DefaultAzureCredential` for the app assertion |
| `pyjwt[crypto]` | Validating the inbound token against Entra's JWKS |
| `httpx` | Async HTTP + SSE against the Gateway |
| `fastapi` / `uvicorn` | The service itself |

No MSAL wrapper needed — `azure-identity` builds on MSAL underneath.

## Sample-specific troubleshooting

| Symptom | Fix |
|---------|-----|
| `401` from this service | Inbound token failed validation. Check `aud` matches `API_AUDIENCE` and `scp` includes `access_as_user`. |
| `403 Unable to obtain Work IQ access` | The OBO exchange failed. Usually missing admin consent on `WorkIQAgent.Ask`, or the user lacks a Copilot license. |
| `401` from the Gateway | The *outbound* token's `aud` is wrong — must be `api://workiq.svc.cloud.microsoft`, not your API. |
| `AADSTS50013: Assertion failed signature validation` | The federated credential subject/issuer doesn't match your managed identity. |
| `502 Work IQ request failed` | The Gateway call failed — network error, timeout, or the Gateway returned a server error. Check connectivity and the `request-id` in logs. |
| Slow first response per turn | Every request does a fresh OBO round trip. See [Notes before production](#notes-before-production). |

See the [root README](../../README.md#troubleshooting) for the full matrix (Copilot license, consent, audience mismatch).

## Notes before production

- **Cache the OBO result.** Each request currently exchanges the token again. Cache on a
  hash of the inbound token, honoring `expires_on`, to save a round trip per turn.
- **The `request-id` response header** is what Microsoft support asks for.
  [`app/workiq.py`](app/workiq.py) includes it in the `WorkIQError` exception;
  [`app/main.py`](app/main.py) logs it when handling errors.
- **Never log tokens.** Errors here are logged with the user's `oid`, not the assertion.

## Resources

- [On-Behalf-Of flow](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-on-behalf-of-flow)
- [Workload identity federation](https://learn.microsoft.com/en-us/entra/workload-id/workload-identity-federation)
- [`azure-identity` for Python](https://learn.microsoft.com/en-us/python/api/overview/azure/identity-readme)
- [Chat API Overview](https://learn.microsoft.com/en-us/microsoft-365-copilot/extensibility/api/ai-services/chat/overview)
- [Work IQ Overview](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/workiq-overview)
