"""The four tools that make this agent worth building.

Everything above these functions — telephony, ASR, TTS, turn-taking — is
commodity you can buy. These are the part that no horizontal voice vendor can
replicate, because they read live TrekOps inventory.

Design rules that matter on a phone call:
  * Return small, speakable dicts. Never raw documents.
  * Never return an ObjectId the model might read aloud; departureId is carried
    for the post-call handoff only, and the prompt forbids saying it.
  * Always return SOMETHING. A tool that raises leaves dead air; a tool that
    returns {"found": false} lets the agent say "let me check another date".
"""

from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema

from app.tenancy import Company
from app.trekops import mongo

# ─────────────────────────────────────────────────────────────────────────────
# Schemas exposed to Gemini
# ─────────────────────────────────────────────────────────────────────────────

check_availability_schema = FunctionSchema(
    name="check_availability",
    description=(
        "Look up upcoming trek departures that still have seats. Call this whenever "
        "the caller asks about dates, seats, availability, or price for a trek. "
        "Always call it before quoting any date or price — never guess."
    ),
    properties={
        "trek_name": {
            "type": "string",
            "description": "Trek name as the caller said it, e.g. 'Kedarkantha'. Omit to list all upcoming departures.",
        },
        "from_date": {
            "type": "string",
            "description": "ISO date (YYYY-MM-DD) the caller is interested in. Omit for 'the next available'.",
        },
        "city": {
            "type": "string",
            "description": "Pickup city the caller wants to board from, e.g. 'Dehradun'.",
        },
    },
    required=[],
)

get_trek_details_schema = FunctionSchema(
    name="get_trek_details",
    description=(
        "Get descriptive detail about a trek — difficulty, altitude, best season, "
        "duration, what it costs. Call this for 'is it hard?', 'how high?', "
        "'is it good for beginners?' style questions."
    ),
    properties={
        "trek_name": {"type": "string", "description": "Trek name as the caller said it."},
    },
    required=["trek_name"],
)

capture_lead_schema = FunctionSchema(
    name="capture_lead",
    description=(
        "Record the caller's interest once you know what they want. Call this as "
        "soon as you have a trek and an approximate date — do NOT wait until the "
        "end of the call, and do not ask for spellings or email. This is what "
        "triggers the WhatsApp follow-up with the itinerary and payment link."
    ),
    properties={
        "name": {"type": "string", "description": "Caller's first name if they gave it."},
        "trek_name": {"type": "string", "description": "The trek they are interested in."},
        "departure_id": {
            "type": "string",
            "description": "The departureId from a previous check_availability result, when they picked a specific date.",
        },
        "group_size": {"type": "integer", "description": "How many people are travelling."},
        "preferred_date": {"type": "string", "description": "ISO date (YYYY-MM-DD) they prefer."},
        "objection": {
            "type": "string",
            "description": "The main hesitation they voiced — price, fitness, dates, permission from family.",
        },
    },
    required=["trek_name"],
)

send_whatsapp_details_schema = FunctionSchema(
    name="send_whatsapp_details",
    description=(
        "Send the full itinerary, price breakdown and booking link to the caller on "
        "WhatsApp. Call this when they say yes, ask you to 'send details', or when "
        "you are wrapping up. Tell them it is on the way, then say goodbye."
    ),
    properties={
        "trek_name": {"type": "string", "description": "The trek to send details for."},
        "departure_id": {
            "type": "string",
            "description": "departureId from check_availability, if a specific date was chosen.",
        },
    },
    required=["trek_name"],
)

TOOLS = ToolsSchema(
    standard_tools=[
        check_availability_schema,
        get_trek_details_schema,
        capture_lead_schema,
        send_whatsapp_details_schema,
    ]
)


# ─────────────────────────────────────────────────────────────────────────────
# Handlers
#
# Bound per call to a CallSession, so the LLM never has to pass tenant or caller
# identity — it cannot leak another company's inventory even if it tries.
# ─────────────────────────────────────────────────────────────────────────────


def build_handlers(session):
    """Return {tool_name: async handler} closed over this call's CallSession."""

    company: Company = session.company

    async def handle_check_availability(params):
        args = params.arguments
        result = await mongo.check_availability(
            company,
            trek_name=args.get("trek_name"),
            from_date=args.get("from_date"),
            city=args.get("city"),
        )
        session.log_tool("check_availability", args, result)
        await params.result_callback(result)

    async def handle_get_trek_details(params):
        result = await mongo.get_trek_details(company, params.arguments.get("trek_name", ""))
        session.log_tool("get_trek_details", params.arguments, result)
        await params.result_callback(result)

    async def handle_capture_lead(params):
        args = params.arguments
        # Held on the session and written once at call end, so a caller who
        # changes their mind twice produces one lead, not three.
        session.lead.update({k: v for k, v in args.items() if v is not None})
        session.log_tool("capture_lead", args, {"captured": True})
        await params.result_callback({"captured": True})

    async def handle_send_whatsapp_details(params):
        args = params.arguments
        session.lead.update({k: v for k, v in args.items() if v is not None})
        session.wants_whatsapp = True
        session.log_tool("send_whatsapp_details", args, {"queued": True})
        # Deliberately queued rather than sent inline: an outbound Meta API call
        # mid-conversation adds latency the caller hears. postcall.py sends it
        # the moment the call ends, which is a second or two later at most.
        await params.result_callback(
            {"queued": True, "message": "Details will be sent on WhatsApp right after this call."}
        )

    return {
        "check_availability": handle_check_availability,
        "get_trek_details": handle_get_trek_details,
        "capture_lead": handle_capture_lead,
        "send_whatsapp_details": handle_send_whatsapp_details,
    }
