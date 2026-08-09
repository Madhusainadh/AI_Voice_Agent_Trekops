# Wiring the calling agent into `WhatsApp_ChatBot_Trek`

> **Status: APPLIED** on 2026-08-09 to the local `Deploy/V2` working tree,
> uncommitted. This document now records what was actually changed, including two
> corrections found while applying it. Keep it in sync if you re-do this on
> another checkout.

Four changes. None of them alter existing behaviour when
`VOICE_AGENT_URL` is unset — the AI simply never answers.

---

## 1. Capture the raw webhook body — `app.js`

Pipecat re-validates Meta's `x-hub-signature-256` against the exact bytes Meta
signed, so a re-serialised body fails. Replace:

```js
app.use(express.json());
```

with:

```js
app.use(express.json({
  // Retain the raw bytes so call webhooks can be forwarded to the voice agent
  // with their Meta signature intact.
  verify: (req, _res, buf) => { req.rawBody = buf; },
}));
```

This is additive — nothing else reads `req.rawBody`.

---

## 2. Schedule the AI answer — `src/routes/webhook.js`

Copy `forwardCallToAgent.js` into `src/whatsapp/`, then in the existing
`calls` handler, inside the `connect` branch — right after the
`emitToCompany(tenantId, "incomingCall", payload)` call, so the dashboard and the
FCM push still fire first:

```js
const voiceAgent = require("../whatsapp/forwardCallToAgent.js");

// … existing incomingCall emit and Firebase push …

// Give a human agent HUMAN_GRACE_SECONDS to pick up; then the AI answers.
if (direction === "inbound") {
  voiceAgent.scheduleAnswer(callId, req.rawBody, req.headers["x-hub-signature-256"]);
}
```

And in the `terminate` branch, so a caller who hangs up while ringing doesn't get
called back by a bot:

```js
voiceAgent.cancel(callId, "call terminated");
```

---

## 3. Let a human beat the AI — `src/routes/calls.js`

At the top of `POST /answer`, before the Meta call:

```js
const voiceAgent = require("../whatsapp/forwardCallToAgent.js");
voiceAgent.cancel(callId, "human agent answered");
```

Same for `POST /reject` — a rejected call should not be picked up by the AI.

---

## 4. New internal routes for the write path — `src/routes/voiceAgent.js`

The agent reads Mongo directly (fast, read-only) but writes through here. Auth
mirrors the existing `x-extension-token` shared-secret pattern in
`src/routes/chat.js` — the agent is not a logged-in user.

```js
"use strict";
const express = require("express");
const { getCompanyConnection } = require("../db/tenantConnectionManager.js");
const { getModel } = require("../db/getModel.js");
const { sendTemplateMessage } = require("../whatsapp/sendMessage.js");

const router = express.Router();

function requireAgentToken(req, res, next) {
  const secret = process.env.AGENT_FORWARD_SECRET;
  if (!secret || req.headers["x-agent-token"] !== secret) {
    return res.status(401).json({ success: false, error: "unauthorized" });
  }
  req.companyCode = (req.headers["x-company-code"] || "").toLowerCase();
  if (!req.companyCode) {
    return res.status(400).json({ success: false, error: "x-company-code required" });
  }
  next();
}

// POST /api/voice-agent/lead — persist the outcome of an AI-handled call.
router.post("/lead", requireAgentToken, async (req, res) => {
  try {
    const db   = await getCompanyConnection(req.companyCode);
    const Call = getModel(db, "Call");
    const { callId, ...rest } = req.body;

    await Call.findOneAndUpdate(
      { callId },
      { $set: { aiHandled: true, aiOutcome: rest } },
      { upsert: true }
    );

    // TODO: upsert into Contact / your lead pipeline here, using whatever the
    // WhatsApp flow already does for an enquiry, so AI leads land in the same
    // funnel as WhatsApp leads rather than a parallel one.

    res.json({ success: true });
  } catch (err) {
    console.error("✗ voice-agent/lead:", err.message);
    res.status(500).json({ success: false, error: err.message });
  }
});

// POST /api/voice-agent/handoff — the WhatsApp follow-up that closes the sale.
router.post("/handoff", requireAgentToken, async (req, res) => {
  try {
    const { phone, template, languageCode = "en", params = {}, paramOrder } = req.body;
    // NOTE: the real export is sendTemplateMessage({ to, name, languageCode,
    // components, tenantId }) — `tenantId` is the companyCode. Body variables are
    // positional, so params are ordered by `paramOrder` (default name, trekName).
    const order = Array.isArray(paramOrder) && paramOrder.length ? paramOrder : ["name", "trekName"];
    const values = order.map((key) => String(params[key] ?? ""));
    await sendTemplateMessage({
      to: phone,
      name: template,
      languageCode,
      components: values.length ? [{ type: "body", parameters: values.map((text) => ({ type: "text", text })) }] : [],
      tenantId: req.companyCode,
    });
    res.json({ success: true });
  } catch (err) {
    console.error("✗ voice-agent/handoff:", err.message);
    res.status(500).json({ success: false, error: err.message });
  }
});

module.exports = router;
```

Mount it in `app.js` next to the other routes:

```js
app.use("/api/voice-agent", require("./src/routes/voiceAgent.js"));
```

> **Corrections found while applying this:**
>
> 1. `sendMessage.js` exports `sendTemplateMessage({ to, name, languageCode,
>    components, tenantId })` — there is no `sendTemplate`. Template body
>    variables are **positional**, so the route maps named params through an
>    explicit `paramOrder`.
> 2. The lead is stored on the `Call` document only. Fanning it into the existing
>    funnel was left as a marked TODO on purpose: `Customer` requires `name` +
>    `email` (a voice call reliably yields neither) and `FunnelEvent.stage` has no
>    `call` value. Pick the target shape and extend the model rather than forcing
>    a fit.
>
> The template you name must already be **approved** in that company's WABA — an
> unknown name fails at Meta, not here.

---

## 5. Extend the `Call` model — `src/models/Call.js`

```js
aiHandled: { type: Boolean, default: false },
aiOutcome: { type: Object,  default: null },
duration:  { type: Number,  default: null },   // referenced by webhook.js already
```

`duration` is written by the existing terminate handler but is not declared on
the schema, so it is currently silently dropped under `strict` mode.

---

## Node `.env` additions

```
VOICE_AGENT_URL=http://127.0.0.1:8090
AGENT_FORWARD_SECRET=<same value as the agent's .env>
HUMAN_GRACE_SECONDS=12
```
