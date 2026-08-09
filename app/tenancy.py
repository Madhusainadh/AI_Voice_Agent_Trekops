"""Resolve which company owns an incoming call.

This is the Python mirror of src/integrations/whatsapp.js:resolveTenantByPhoneNumberId
in the Node backend. Meta identifies the receiving business number with a
`phone_number_id`; the control-plane `integrationconfigs` collection maps that to
a companyCode, and `companies` maps the code to the per-tenant database name.

Both lookups are cached for CACHE_TTL because they are on the call-answer path and
a Mongo round-trip there is audible.
"""

import time
from dataclasses import dataclass

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorClient

from app.config import settings

CACHE_TTL = 300  # seconds — matches the Node cache

_client: AsyncIOMotorClient | None = None
_cache: dict[str, tuple[float, "Company | None"]] = {}


@dataclass(frozen=True)
class Company:
    code: str
    name: str
    db_name: str
    status: str


def get_client() -> AsyncIOMotorClient:
    """Shared Motor client. Created lazily so importing this module is cheap."""
    global _client
    if _client is None:
        if not settings.mongo_uri:
            raise RuntimeError("MONGO_URI is not set — cannot resolve tenants or read departures")
        _client = AsyncIOMotorClient(settings.mongo_uri, serverSelectionTimeoutMS=3000)
    return _client


async def resolve_company_by_phone_number_id(phone_number_id: str) -> Company | None:
    """Return the Company that owns this Meta phone_number_id, or None."""
    if not phone_number_id:
        return None

    hit = _cache.get(phone_number_id)
    if hit and time.time() < hit[0]:
        return hit[1]

    control = get_client()[settings.mongo_control_db]
    company: Company | None = None
    try:
        cfg = await control.integrationconfigs.find_one(
            {"provider": "whatsapp", "enabled": True, "meta.phoneNumberId": phone_number_id},
            {"companyCode": 1},
        )
        if cfg:
            doc = await control.companies.find_one(
                {"code": cfg["companyCode"]}, {"code": 1, "name": 1, "dbName": 1, "status": 1}
            )
            if doc:
                company = Company(
                    code=doc["code"],
                    name=doc.get("name", doc["code"]),
                    db_name=doc["dbName"],
                    status=doc.get("status", "active"),
                )
    except Exception as err:  # never let tenant resolution kill the call path
        logger.warning(f"tenant resolution failed for phone_number_id={phone_number_id}: {err}")

    _cache[phone_number_id] = (time.time() + CACHE_TTL, company)
    return company


def invalidate_cache() -> None:
    _cache.clear()
