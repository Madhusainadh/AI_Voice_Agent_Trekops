"""Write-path client for the TrekOps Node backend.

Everything that MUTATES state goes through here rather than through Mongo, so the
Node backend stays the single owner of business logic (lead dedupe, WhatsApp
credential resolution, template rendering, socket fan-out).

TWO AUTH MODES, matching what already exists in the Node codebase:

  * `x-agent-token` shared secret — for the new /api/voice-agent/* routes this
    service needs. Mirrors the existing `x-extension-token` pattern in
    src/routes/chat.js, which authenticates a non-user client the same way.
  * Bearer JWT — for EXISTING routes behind requireAuthRest (e.g.
    /api/calls/terminate). We mint a short-lived token with the backend's
    JWT_SECRET rather than storing a 7-day user token on disk. Payload shape is
    taken from src/middleware/auth.js:generateToken.
"""

import time

import httpx
import jwt
from loguru import logger

from app.config import settings

_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        # 4s ceiling: this is off the critical speech path (post-call work), but a
        # hung backend must never wedge the agent process.
        _client = httpx.AsyncClient(base_url=settings.trekops_api_url, timeout=4.0)
    return _client


def mint_service_token(company_code: str, ttl_seconds: int = 300) -> str:
    """Short-lived JWT the Node authMiddleware will accept as an admin user."""
    if not settings.jwt_secret:
        raise RuntimeError("JWT_SECRET is not set — cannot call authenticated TrekOps routes")
    return jwt.encode(
        {
            "userId": settings.service_user_id,
            "companyCode": company_code.lower(),
            "role": "admin",
            "exp": int(time.time()) + ttl_seconds,
        },
        settings.jwt_secret,
        algorithm="HS256",
    )


def _agent_headers(company_code: str) -> dict:
    return {
        "x-agent-token": settings.agent_forward_secret,
        "x-company-code": company_code,
        "Content-Type": "application/json",
    }


async def create_lead(company_code: str, payload: dict) -> dict | None:
    """Persist the structured outcome of a call. Requires the Node-side patch.

    payload: { phone, callId, name?, trekName?, departureId?, groupSize?,
               preferredDate?, objection?, outcome, transcript, durationSeconds }
    """
    try:
        r = await _http().post(
            "/api/voice-agent/lead", json=payload, headers=_agent_headers(company_code)
        )
        r.raise_for_status()
        return r.json()
    except Exception as err:
        logger.error(f"create_lead failed for {payload.get('phone')}: {err}")
        return None


async def send_whatsapp_followup(company_code: str, phone: str, template: str, params: dict) -> bool:
    """Hand the conversation back to WhatsApp — the channel that actually closes.

    The Node side owns which template is approved and how it renders; we only name
    it and supply the variables.
    """
    try:
        r = await _http().post(
            "/api/voice-agent/handoff",
            json={"phone": phone, "template": template, "params": params},
            headers=_agent_headers(company_code),
        )
        r.raise_for_status()
        return True
    except Exception as err:
        logger.error(f"send_whatsapp_followup failed for {phone}: {err}")
        return False


async def terminate_call(company_code: str, call_id: str) -> bool:
    """Hang up via the existing /api/calls/terminate route (JWT-authenticated)."""
    try:
        token = mint_service_token(company_code)
        r = await _http().post(
            "/api/calls/terminate",
            json={"callId": call_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        r.raise_for_status()
        return True
    except Exception as err:
        logger.error(f"terminate_call failed for {call_id}: {err}")
        return False


async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
