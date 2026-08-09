# Trek Calling Agent

AI voice agent for inbound trek sales calls, grounded in live TrekOps inventory.

Companion service to [`WhatsApp_ChatBot_Trek`](../WhatsApp_ChatBot_Trek). It runs
as a **separate Python process** — it holds long-lived WebRTC connections and
does per-packet audio work, neither of which belongs inside the Node API.

---

## What it does

A trekker calls the operator's WhatsApp business number. If no human picks up
within ~12 seconds, this service answers, has a real conversation grounded in
actual departures and seat counts, captures the lead, and hands off to WhatsApp
for the itinerary and payment link.

**It does not close the booking on the call.** Voice is bad at spelling names,
confirming dates and taking money; WhatsApp is already the best closing channel
in the product. Voice does persuasion, WhatsApp does paperwork.

---

## The stack

| Layer | Choice | Note |
|---|---|---|
| Transport | **WhatsApp Business Calling** (WebRTC) | No PSTN, no telephony vendor, no per-minute carrier cost |
| Orchestration | **Pipecat** | VAD, turn-taking, barge-in |
| ASR | **Sarvam Saaras v3**, `mode="codemix"` | Survives Hinglish mid-sentence switching |
| LLM | **Gemini Flash** | Same provider the WhatsApp bot already meters |
| TTS | **Sarvam Bulbul v2** | v3 is better and ~2× the cost |
| Grounding | TrekOps Mongo (read) + Node API (write) | The part nobody else can copy |

---

## Quick start

```bash
cp .env.example .env      # fill in Sarvam, Google, Meta and Mongo values
./run.sh                  # creates .venv, installs deps, starts on :8090
```

Then either point Meta's `calls` webhook straight at `https://<host>/whatsapp`
(pilot), or apply [`integration/NODE_PATCH.md`](integration/NODE_PATCH.md) so the
Node backend forwards unanswered calls (production — keeps the human-agent
dashboard working).

On the Meta side you need: **Allow voice calls** enabled for the phone number,
and the **`calls`** webhook field subscribed.

---

## Layout

```
app/
  server.py          FastAPI — webhook in, WebRTC out
  bot.py             one Pipecat pipeline per call + CallSession state
  prompts.py         system prompt, written for speech not chat
  postcall.py        transcript → lead → WhatsApp handoff
  tenancy.py         phone_number_id → company → tenant DB
  tools/definitions.py   the four grounding tools
  trekops/mongo.py   read path — direct Mongo, latency-critical
  trekops/api.py     write path — Node API, business logic lives there
integration/         the Node-side changes this service needs
```

---

## The four tools

| Tool | Reads | Why it exists |
|---|---|---|
| `check_availability` | `departures` + `treks` | Real dates, real seats, real prices |
| `get_trek_details` | `treks` | Difficulty, altitude, season |
| `capture_lead` | — (session) | Fires as soon as intent is known, not at hangup |
| `send_whatsapp_details` | — (queued) | Triggers the close on WhatsApp |

Reads go direct to Mongo because they sit inside the turn loop and an extra HTTP
hop is audible. Writes go through the Node API so lead handling and WhatsApp
credentials stay in one place.

---

## Status — read this before running it

Scaffold, not a tested deployment. Written against the current TrekOps schemas
and verified Pipecat/Sarvam API signatures, but **nothing here has been run
against a live call.** Before a pilot:

1. `pip install -r requirements.txt` and fix any import drift — Pipecat has
   reorganised its transport modules more than once; `bot.py` carries shims for
   the layouts in circulation but does not cover every version.
2. There is an open upstream issue about the Sarvam integration
   ([pipecat#3783](https://github.com/pipecat-ai/pipecat/issues/3783)) — check it
   resolves against the version you pin.
3. `sendMessage.sendTemplate(...)` in the Node patch is the one call not verified
   against the current module exports. See the note in `NODE_PATCH.md`.
4. The WhatsApp follow-up template (`voice_call_followup`) must be approved in
   your WABA first.
5. Add an AI disclosure + call-recording line to `/privacy-policy` before any
   real caller reaches this.

---

## Cost

Roughly **₹2–3/minute** all-in — and lower than the original PSTN plan, because
WhatsApp Calling removes the telephony leg entirely.

| Component | ≈ ₹/min |
|---|---|
| Sarvam Saaras (STT) | 0.50 |
| Sarvam Bulbul v2 (TTS) | ~0.70 |
| Gemini Flash | ~0.15 |
| Telephony | **0** — WebRTC over WhatsApp |

A 4-minute call costs ~₹6–10. Meter it on top of the plan; never bundle it.
Sarvam runs a startup programme (6–12 months of credits) worth applying to before
you burn paid credits on the pilot.
