"""Everything that happens after the caller hangs up.

Kept strictly off the call path — none of this is latency-sensitive, and all of
it is allowed to fail without affecting the conversation that already happened.

Flow:  transcript  →  lead record in TrekOps  →  WhatsApp follow-up  →  done
"""

from loguru import logger

from app.trekops import api


def summarise_outcome(session) -> str:
    """Cheap heuristic label — no second LLM call, no extra cost or latency.

    The full transcript goes to the lead record, so a human (or a batch job) can
    always re-derive something richer later.
    """
    if session.wants_whatsapp and session.lead.get("trek_name"):
        return "interested_details_sent"
    if session.lead.get("trek_name"):
        return "interested"
    if session.turn_count <= 2:
        return "abandoned"
    return "enquiry_no_intent"


async def run(session) -> None:
    """Persist the call and fire the WhatsApp handoff."""
    outcome = summarise_outcome(session)
    lead = session.lead

    payload = {
        "phone": session.caller_phone,
        "callId": session.call_id,
        "name": lead.get("name"),
        "trekName": lead.get("trek_name"),
        "departureId": lead.get("departure_id"),
        "groupSize": lead.get("group_size"),
        "preferredDate": lead.get("preferred_date"),
        "objection": lead.get("objection"),
        "outcome": outcome,
        "transcript": session.transcript_text(),
        "durationSeconds": session.duration_seconds(),
        "toolCalls": session.tool_calls,
    }

    logger.info(
        f"[postcall] call={session.call_id} outcome={outcome} "
        f"trek={lead.get('trek_name')} turns={session.turn_count}"
    )

    await api.create_lead(session.company.code, payload)

    # Only follow up when the caller actually asked for it. An unrequested
    # template on a 15-second wrong-number call is how a WABA gets rate-limited.
    if session.wants_whatsapp and lead.get("trek_name"):
        await api.send_whatsapp_followup(
            session.company.code,
            session.caller_phone,
            template="voice_call_followup",
            params={
                "trekName": lead.get("trek_name"),
                "departureId": lead.get("departure_id"),
                "name": lead.get("name") or "",
            },
        )
