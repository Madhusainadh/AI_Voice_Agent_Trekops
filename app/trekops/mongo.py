"""Read-only grounding queries against a company's TrekOps database.

WHY DIRECT MONGO INSTEAD OF THE NODE API
────────────────────────────────────────
These queries sit inside the turn loop: the caller asks "Kedarkantha me 20 ko
seat hai?" and every millisecond before the reply is dead air. Going through the
Node API would add an HTTP hop plus GraphQL resolution on top of the same Mongo
read. So READS come straight from Mongo, and every WRITE goes through the Node
API (see api.py) where the business logic and tenant guards live.

Field names below are taken from the Mongoose schemas in
WhatsApp_ChatBot_Trek/src/models/{treks,Departure}.js — keep them in sync.
Collections use Mongoose's default pluralisation: Trek → "treks", Departure →
"departures", Contact → "contacts".
"""

from datetime import datetime, timedelta, timezone

from loguru import logger

from app.tenancy import Company, get_client

# Departure.status values that still accept bookings.
BOOKABLE = ["Open", "Almost Full"]


def _db(company: Company):
    return get_client()[company.db_name]


def _seats_left(dep: dict) -> int:
    return max(0, int(dep.get("capacity") or 0) - int(dep.get("booked") or 0))


def _fmt_date(value) -> str:
    if isinstance(value, datetime):
        return value.strftime("%d %b %Y")
    return str(value or "")


def _shape_departure(dep: dict) -> dict:
    """Flatten a departure into the small dict the LLM sees.

    Deliberately narrow: the model only gets what it can say out loud. Dumping the
    whole document wastes context and invites the agent to read out ObjectIds.
    """
    return {
        "departureId": str(dep.get("_id")),
        "trekName": dep.get("trekName"),
        "startDate": _fmt_date(dep.get("startDate")),
        "endDate": _fmt_date(dep.get("endDate")),
        "duration": dep.get("duration") or f"{dep.get('days') or 0}D/{dep.get('nights') or 0}N",
        "price": dep.get("price"),
        "seatsLeft": _seats_left(dep),
        "status": dep.get("status"),
        "pickupCity": dep.get("cityName"),
        "pickupTime": dep.get("pickupTime"),
        "meetingPoint": dep.get("meetingPoint"),
        "acceptsPartialPayment": bool(dep.get("acceptPartialPayment")),
        "partialAmount": dep.get("partialPaymentAmount"),
    }


async def find_trek(company: Company, query: str) -> dict | None:
    """Fuzzy-match a trek by name or short name. Case-insensitive, prefix-anchored."""
    if not query:
        return None
    safe = query.strip()
    doc = await _db(company).treks.find_one(
        {
            "isActive": {"$ne": False},
            "$or": [
                {"name": {"$regex": safe, "$options": "i"}},
                {"shortName": {"$regex": safe, "$options": "i"}},
            ],
        }
    )
    return doc


async def check_availability(
    company: Company,
    trek_name: str | None = None,
    from_date: str | None = None,
    city: str | None = None,
    limit: int = 4,
) -> dict:
    """Upcoming bookable departures, optionally filtered by trek / city / date.

    Returns at most `limit` — a voice agent reading out eight dates is a hang-up.
    """
    now = datetime.now(timezone.utc)
    criteria: dict = {
        "isDeleted": {"$ne": True},
        "status": {"$in": BOOKABLE},
        "startDate": {"$gte": now},
    }

    if from_date:
        try:
            parsed = datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc)
            # Widen to a window around the asked-for date: callers say "around the
            # 20th" and the useful answer includes the 18th and the 22nd.
            criteria["startDate"] = {"$gte": parsed - timedelta(days=4)}
        except ValueError:
            logger.warning(f"check_availability: unparseable from_date={from_date!r}, ignoring")

    if trek_name:
        trek = await find_trek(company, trek_name)
        if not trek:
            return {"found": False, "reason": "trek_not_found", "query": trek_name, "departures": []}
        criteria["trekId"] = trek["_id"]

    if city:
        criteria["$or"] = [
            {"cityName": {"$regex": city, "$options": "i"}},
            {"cityPickups.cityName": {"$regex": city, "$options": "i"}},
        ]

    cursor = _db(company).departures.find(criteria).sort("startDate", 1).limit(limit)
    departures = [_shape_departure(d) async for d in cursor]

    # A departure can be flagged Open but be sold out; don't offer those.
    departures = [d for d in departures if d["seatsLeft"] > 0]

    return {"found": bool(departures), "departures": departures}


async def get_trek_details(company: Company, trek_name: str) -> dict:
    """Descriptive detail for a trek — difficulty, altitude, season, price band."""
    trek = await find_trek(company, trek_name)
    if not trek:
        return {"found": False, "query": trek_name}
    return {
        "found": True,
        "name": trek.get("name"),
        "location": trek.get("location"),
        "difficulty": trek.get("difficulty"),
        "altitude": trek.get("altitude"),
        "bestSeason": trek.get("bestSeason"),
        "duration": trek.get("duration"),
        "startFrom": trek.get("startFrom"),
        "price": trek.get("price"),
        # description/itinerary are long-form; trimmed so TTS never reads an essay.
        "description": (trek.get("description") or "")[:600],
    }


async def get_caller_context(company: Company, phone: str) -> dict:
    """Who is calling — existing contact and any live booking.

    Lets the agent open with "Hi Rahul, calling about your Brahmatal booking?"
    instead of interrogating a customer it already knows.
    """
    db = _db(company)
    ctx: dict = {"known": False}
    try:
        contact = await db.contacts.find_one({"phone": phone}, {"name": 1, "phone": 1})
        if contact:
            ctx.update({"known": True, "name": contact.get("name")})

        booking = await db.bookings.find_one(
            {"phone": phone}, {"trekName": 1, "status": 1, "createdAt": 1}, sort=[("createdAt", -1)]
        )
        if booking:
            ctx["lastBooking"] = {
                "trekName": booking.get("trekName"),
                "status": booking.get("status"),
            }
    except Exception as err:
        logger.warning(f"caller context lookup failed for {phone}: {err}")
    return ctx
