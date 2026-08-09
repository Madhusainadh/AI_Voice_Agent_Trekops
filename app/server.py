"""FastAPI front door for the calling agent.

TWO WAYS A CALL REACHES THIS SERVICE
────────────────────────────────────
1. DIRECT (simplest, pilot): Meta's `calls` webhook points straight here.
   Fastest path to a working demo, but the Node backend then never sees call
   events, so the human-agent dashboard goes dark. Use for a single pilot tenant.

2. FORWARDED (recommended, production): Meta keeps posting to the Node backend,
   which continues to drive the dashboard, the FCM push and the Call collection —
   and forwards the raw call payload here when no human picks up. See
   integration/NODE_PATCH.md. This preserves every existing behaviour and lets a
   human beat the AI to the call.

Both land on the same handler, because Pipecat's WhatsAppClient only needs the
raw Meta body plus the signature header to establish WebRTC.
"""

import asyncio
from contextlib import asynccontextmanager

import aiohttp
from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from loguru import logger
from pipecat.transports.whatsapp.api import WhatsAppWebhookRequest
from pipecat.transports.whatsapp.client import WhatsAppClient

from app.bot import run_bot
from app.config import settings
from app.tenancy import resolve_company_by_phone_number_id
from app.trekops import api

_session: aiohttp.ClientSession | None = None
_whatsapp: WhatsAppClient | None = None

# Calls currently being handled, so a duplicate webhook delivery (Meta retries)
# doesn't start a second pipeline on the same call.
_active: set[str] = set()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _session, _whatsapp
    _session = aiohttp.ClientSession()
    _whatsapp = WhatsAppClient(
        whatsapp_token=settings.whatsapp_token,
        phone_number_id=settings.whatsapp_phone_number_id,
        session=_session,
        whatsapp_secret=settings.whatsapp_app_secret,
    )
    logger.info(f"calling agent up on :{settings.port}")
    yield
    if _whatsapp:
        await _whatsapp.terminate_all_calls()
    if _session:
        await _session.close()
    await api.aclose()


app = FastAPI(title="Trek Calling Agent", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"ok": True, "activeCalls": len(_active)}


@app.get("/whatsapp")
async def verify_webhook(request: Request):
    """Meta's webhook verification handshake (mode 1 only)."""
    status = await _whatsapp.handle_verify_webhook_request(
        dict(request.query_params), settings.whatsapp_webhook_verification_token
    )
    if status == 200:
        return PlainTextResponse(request.query_params.get("hub.challenge", ""))
    return Response(status_code=status)


@app.post("/whatsapp")
async def whatsapp_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
):
    """Mode 1: Meta posts call events directly to this service."""
    raw = await request.body()
    body = WhatsAppWebhookRequest.model_validate_json(raw)
    handled = await _whatsapp.handle_webhook_request(
        body,
        connection_callback=_on_connection,
        raw_body=raw,
        sha256_signature=x_hub_signature_256,
    )
    return JSONResponse({"handled": handled})


@app.post("/internal/forwarded-call")
async def forwarded_call(
    request: Request,
    x_agent_token: str | None = Header(default=None),
    x_hub_signature_256: str | None = Header(default=None),
):
    """Mode 2: the Node backend forwards a call event it decided not to answer.

    The Node side must forward the RAW request body byte-for-byte along with the
    original x-hub-signature-256 header, or Meta's signature check here fails.
    """
    if not settings.agent_forward_secret or x_agent_token != settings.agent_forward_secret:
        logger.warning("rejected forwarded call: bad or missing x-agent-token")
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    raw = await request.body()
    body = WhatsAppWebhookRequest.model_validate_json(raw)
    handled = await _whatsapp.handle_webhook_request(
        body,
        connection_callback=_on_connection,
        raw_body=raw,
        sha256_signature=x_hub_signature_256,
    )
    return JSONResponse({"handled": handled})


async def _on_connection(connection, call) -> None:
    """Pipecat has a live WebRTC connection — start the bot on it.

    `call` carries the Meta call: call.id, call.from_ (caller wa_id), call.session.
    """
    call_id = getattr(call, "id", "unknown")
    caller_phone = getattr(call, "from_", None) or getattr(call, "from", None) or ""

    if call_id in _active:
        logger.info(f"[{call_id}] duplicate webhook delivery — ignoring")
        return

    company = await resolve_company_by_phone_number_id(settings.whatsapp_phone_number_id)
    if company is None:
        logger.error(
            f"[{call_id}] no company owns phone_number_id={settings.whatsapp_phone_number_id} — "
            "check IntegrationConfig.meta.phoneNumberId in the control-plane DB"
        )
        return

    if not settings.company_allowed(company.code):
        logger.warning(f"[{call_id}] company {company.code} not in ENABLED_COMPANY_CODES — declining")
        return

    _active.add(call_id)

    async def _run():
        try:
            await asyncio.wait_for(
                run_bot(connection, call_id, caller_phone, company),
                timeout=settings.max_call_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning(f"[{call_id}] hit MAX_CALL_SECONDS — terminating")
            await api.terminate_call(company.code, call_id)
        finally:
            _active.discard(call_id)

    # Detached: the webhook must return to Meta immediately, not after the call.
    asyncio.create_task(_run())


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=settings.port, log_level=settings.log_level.lower())
