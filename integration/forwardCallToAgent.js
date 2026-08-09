"use strict";
/**
 * Node → Python bridge for AI call answering.
 *
 * Drop this file into WhatsApp_ChatBot_Trek/src/whatsapp/ and wire it per
 * integration/NODE_PATCH.md.
 *
 * The contract is deliberately small: when an inbound call rings, we start a
 * grace timer. If a human agent answers (POST /api/calls/answer) before it
 * fires, we cancel. If it fires, we forward the ORIGINAL raw webhook body and
 * signature to the Python agent, which establishes WebRTC and answers.
 *
 * Forwarding the raw body matters: Pipecat re-validates Meta's
 * x-hub-signature-256 against the exact bytes Meta signed. A re-serialised
 * JSON.stringify(req.body) will not match.
 */

const axios = require("axios");

const AGENT_URL     = process.env.VOICE_AGENT_URL || "";
const AGENT_SECRET  = process.env.AGENT_FORWARD_SECRET || "";
const GRACE_SECONDS = parseInt(process.env.HUMAN_GRACE_SECONDS || "12", 10);

// callId -> NodeJS.Timeout
const _pending = new Map();

function enabled() {
  return Boolean(AGENT_URL && AGENT_SECRET);
}

/**
 * Cancel a pending AI answer because a human got there first.
 * Safe to call for unknown callIds.
 */
function cancel(callId, reason = "human answered") {
  const timer = _pending.get(callId);
  if (!timer) return false;
  clearTimeout(timer);
  _pending.delete(callId);
  console.log(`[voice-agent] cancelled AI answer for ${callId} — ${reason}`);
  return true;
}

/**
 * Schedule the AI to answer this call unless a human beats it.
 *
 * @param {string} callId
 * @param {Buffer} rawBody    — req.rawBody, captured by the express.json verify hook
 * @param {string} signature  — the incoming x-hub-signature-256 header
 */
function scheduleAnswer(callId, rawBody, signature) {
  if (!enabled()) return;
  if (!rawBody) {
    console.warn("[voice-agent] no rawBody captured — see NODE_PATCH.md step 1; skipping forward");
    return;
  }
  if (_pending.has(callId)) return;

  const timer = setTimeout(async () => {
    _pending.delete(callId);
    try {
      await axios.post(`${AGENT_URL}/internal/forwarded-call`, rawBody, {
        headers: {
          "Content-Type": "application/json",
          "x-agent-token": AGENT_SECRET,
          ...(signature ? { "x-hub-signature-256": signature } : {}),
        },
        timeout: 5000,
      });
      console.log(`[voice-agent] forwarded ${callId} to AI after ${GRACE_SECONDS}s`);
    } catch (err) {
      // A dead agent service must never break the human call flow — the call is
      // still ringing on the dashboard and a human can still pick it up.
      console.error(`[voice-agent] forward failed for ${callId}:`, err.response?.data || err.message);
    }
  }, GRACE_SECONDS * 1000);

  _pending.set(callId, timer);
  console.log(`[voice-agent] AI will answer ${callId} in ${GRACE_SECONDS}s unless a human picks up`);
}

module.exports = { scheduleAnswer, cancel, enabled };
