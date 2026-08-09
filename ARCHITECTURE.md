# Architecture & decisions

## What changed from the original plan

The plan drafted on 4 Aug assumed **PSTN**: a Plivo virtual number, conditional
call forwarding from the operator's existing line, and a per-minute carrier cost.

Reading the codebase invalidated that. `WhatsApp_ChatBot_Trek` already ships a
complete **WhatsApp Business Calling** implementation — `src/routes/calls.js`
(enable, permissions, initiate, answer, reject, terminate), a `Call` model, SDP
offer/answer plumbing, `incomingCall` over Socket.IO, and FCM push to human
agents. That is WebRTC, not telephony.

So:

| | Original plan (4 Aug) | Now |
|---|---|---|
| Transport | Plivo PSTN + forwarding | WhatsApp Calling (WebRTC) |
| Telephony cost | ₹0.35–0.60/min | **₹0** |
| Operator setup | Configure call forwarding | Nothing — already their WhatsApp number |
| Caller reach | Anyone with a phone | Only people who call via WhatsApp |
| Build already done | None | Signalling, permissions, call records, dashboard |

The trade is reach: WhatsApp Calling only serves callers who dial through
WhatsApp. Given the product is "WhatsApp-first CRM", that is the right side of
the trade for v1 — and the Plivo path stays open later for callers on a normal
phone line, reusing this same pipeline behind a different transport.

---

## Call flow

```
Trekker taps "call" in WhatsApp
        │
        ▼
   Meta Cloud API  ──POST /api/webhook──▶  Node backend (existing, unchanged)
                                              │
                                              ├─▶ Call doc created (status: ringing)
                                              ├─▶ socket "incomingCall" → dashboard
                                              ├─▶ FCM push → human agents
                                              │
                                              └─ 12s grace timer ─┐
                                                                  │
        human answers ──▶ POST /api/calls/answer ──▶ timer cancelled, AI stands down
                                                                  │
        nobody answers ───────────────────────────────────────────┘
                                              │
                                              ▼  raw body + x-hub-signature-256
                                    POST /internal/forwarded-call
                                              │
                              ┌───────────────▼───────────────┐
                              │   Trek Calling Agent (Python) │
                              │   Pipecat + SmallWebRTC       │
                              └───────────────┬───────────────┘
                                              │ accepts call via Meta /calls
                                              ▼
                     ┌────────────── WebRTC audio ──────────────┐
                     │                                          │
              Sarvam Saaras (STT, codemix)                      │
                     │                                          │
              Gemini Flash + 4 tools ──▶ Mongo (departures, treks)
                     │                                          │
              Sarvam Bulbul (TTS) ──────────────────────────────┘
                                              │
                                    caller hangs up
                                              ▼
                              transcript → lead → POST /api/voice-agent/lead
                                              └──▶ WhatsApp template
                                                   (itinerary + payment link)
```

---

## Decisions and why

**Separate Python process, not folded into Node.**
Persistent WebRTC connections and per-packet audio work would compete with the
Express event loop that serves the dashboard and webhooks. Pipecat is Python-only
regardless.

**Human wins the race.**
The AI is a fallback, not a replacement. The 12-second grace timer means an
operator who is at their desk never notices the agent exists — and the dashboard,
push notification and call history keep working exactly as they do today. Setting
`HUMAN_GRACE_SECONDS=0` turns it into after-hours-only answering.

**Reads direct from Mongo, writes through the Node API.**
An availability lookup happens mid-sentence; the caller hears every extra
millisecond. Lead creation happens after hangup, where an HTTP hop costs nothing
and keeping business logic in one place is worth a lot.

**Tools are bound per call to a `CallSession`.**
The LLM never passes a tenant or a phone number as an argument, so it cannot be
argued into reading another company's inventory.

**`capture_lead` fires mid-call, not at the end.**
Callers hang up abruptly. A lead captured at second 40 survives a disconnect at
second 41.

**The WhatsApp send is queued, not inline.**
An outbound Meta API call mid-conversation adds latency the caller hears. Queuing
it costs a second or two of delay that nobody notices.

---

## Latency budget

Target under ~1.5s from end-of-speech to first audio.

| Stage | Budget |
|---|---|
| VAD end-of-turn | 200–300ms |
| Sarvam STT final | 150–300ms |
| Gemini Flash first token | 300–600ms |
| Tool call (Mongo, when used) | 30–80ms direct |
| Sarvam TTS first chunk | 200–400ms |

Two things protect this:

1. **Filler lines before tool calls.** The prompt requires "let me check that
   batch for you" before `check_availability`. Silence reads as a dropped call.
2. **Index `departures` for the query the agent hammers**:
   `{ isDeleted: 1, status: 1, startDate: 1 }`, and `{ trekId: 1, startDate: 1 }`.
   Without these the tool call blows the budget on a large tenant.

Run the service in the same region as your Mongo and close to Meta's endpoint —
every hop is audible on a call in a way it never is on WhatsApp.

---

## Open questions

- **Multi-tenant credentials.** The agent currently reads one
  `WHATSAPP_TOKEN` / `WHATSAPP_PHONE_NUMBER_ID` pair from env — fine for a
  single-tenant pilot. Serving several operators means constructing a
  `WhatsAppClient` per company from `IntegrationConfig`, the way
  `getWhatsappCredentials()` already does on the Node side.
- **Recording consent.** The greeting discloses AI and recording, but nothing
  stores audio yet. If you add recording, `/privacy-policy` must change first.
- **Barge-in tuning.** Silero VAD defaults are tuned for English speakers on
  headsets. Expect to tune sensitivity for Indian mobile networks and speakerphone.
- **Cost metering.** `enable_metrics=True` is set but nothing writes per-call cost
  to `AiInteractionLog` yet — worth wiring so voice lands in the same `/ai-usage`
  view as the WhatsApp bot.
