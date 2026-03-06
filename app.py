#!/usr/bin/env python3
"""
Happy Campers Rescue Ranch — AMD Voicemail Webhook Server
Production deployment for Railway.

Single-call flow:
  1. Twilio dials the guest with AMD enabled
  2. /answer fires when call connects:
     - Calls ElevenLabs register-call API to get a TwiML WebSocket stream
     - Returns that TwiML so Arcadio streams in on the SAME call
  3. /amd-callback fires asynchronously:
     - human            → already connected via WebSocket stream (do nothing)
     - machine_end_beep → redirect the call to play the voicemail drop
     - machine_start    → wait for machine_end_* callback
     - fax/unknown      → hang up cleanly

All secrets are loaded from environment variables — never hardcoded.
"""

import os
import logging
import requests as req
from flask import Flask, request, Response

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ─── CONFIG (set as Railway environment variables) ────────────────────────────

TWILIO_ACCOUNT_SID  = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN   = os.environ.get("TWILIO_AUTH_TOKEN")
FROM_NUMBER         = os.environ.get("FROM_NUMBER", "+13528978771")
ELEVENLABS_API_KEY  = os.environ.get("ELEVENLABS_API_KEY")
ELEVENLABS_AGENT_ID = os.environ.get("ELEVENLABS_AGENT_ID")
ELEVENLABS_PHONE_ID = os.environ.get("ELEVENLABS_PHONE_ID", "phnum_4501kjx114q0f8j8cn0c3tt3b0f3")
VOICEMAIL_AUDIO_URL = os.environ.get("VOICEMAIL_AUDIO_URL")

# In-memory store: CallSid -> status ("human" | "machine" | "pending")
call_status_map = {}

# ─── ROUTES ───────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok", "service": "Happy Campers AMD Voicemail Webhook"}, 200


@app.route("/answer", methods=["POST"])
def answer():
    """
    Twilio calls this when the call is answered.
    Calls ElevenLabs register-call to get TwiML WebSocket stream.
    Returns the TwiML so Arcadio connects on the same call.
    """
    call_sid  = request.form.get("CallSid", "unknown")
    to_number = request.form.get("To", "")
    from_number = request.form.get("From", "")

    logger.info(f"📞 Call answered — SID: {call_sid}, To: {to_number}")

    # Mark as pending AMD result
    call_status_map[call_sid] = {"to": to_number, "status": "pending"}

    # Call ElevenLabs register-call to get TwiML for WebSocket stream
    try:
        response = req.post(
            "https://api.elevenlabs.io/v1/convai/twilio/register-call",
            headers={
                "xi-api-key": ELEVENLABS_API_KEY,
                "Content-Type": "application/json"
            },
            json={
                "agent_id": ELEVENLABS_AGENT_ID,
                "call_sid": call_sid,
                "direction": "outbound",
                "from_number": FROM_NUMBER,
                "to_number": to_number,
                "dynamic_variables": {}
            },
            timeout=10
        )

        if response.status_code == 200:
            content_type = response.headers.get("Content-Type", "")
            if "xml" in content_type or response.text.strip().startswith("<"):
                # Response IS the TwiML XML directly
                logger.info(f"   ✅ Got TwiML XML from ElevenLabs for {call_sid}")
                return Response(response.text, mimetype="text/xml")
            else:
                # Try JSON fallback
                try:
                    data = response.json()
                    twiml = data.get("twiml") or data.get("twiML") or data.get("TwiML")
                    if twiml:
                        logger.info(f"   ✅ Got TwiML from JSON for {call_sid}")
                        return Response(twiml, mimetype="text/xml")
                    else:
                        logger.error(f"   ❌ No TwiML in JSON response: {data}")
                except Exception:
                    logger.error(f"   ❌ Could not parse response: {response.text[:200]}")
        else:
            logger.error(f"   ❌ register-call failed: {response.status_code} — {response.text}")

    except Exception as e:
        logger.error(f"   ❌ register-call exception: {e}")

    # Fallback: if register-call fails, play silence and hang up
    fallback_twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>We're sorry, there was a technical issue. Please try again later.</Say>
    <Hangup/>
</Response>"""
    return Response(fallback_twiml, mimetype="text/xml")


@app.route("/amd-callback", methods=["POST"])
def amd_callback():
    """
    Twilio AMD result callback — fires asynchronously after AMD analysis.
    If voicemail detected, redirect the call to play the voicemail drop.
    If human, the WebSocket stream is already connected — do nothing.
    """
    from twilio.rest import Client

    answered_by = request.form.get("AnsweredBy", "unknown")
    call_sid    = request.form.get("CallSid", "unknown")
    to_number   = (call_status_map.get(call_sid) or {}).get("to", request.form.get("To", ""))

    logger.info(f"🔍 AMD — SID: {call_sid}, To: {to_number}, AnsweredBy: {answered_by}")

    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

    if answered_by == "human":
        # WebSocket stream is already connected — Arcadio is talking. Nothing to do.
        logger.info(f"✅ Human confirmed: {to_number} — Arcadio is already on the line")
        if call_sid in call_status_map:
            call_status_map[call_sid]["status"] = "human"
        return "", 204

    elif answered_by in ("machine_end_beep", "machine_end_silence", "machine_end_other"):
        logger.info(f"📬 Voicemail detected: {to_number} — redirecting to voicemail drop")
        try:
            client.calls(call_sid).update(
                twiml=f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Play>{VOICEMAIL_AUDIO_URL}</Play>
    <Hangup/>
</Response>"""
            )
            logger.info(f"   ✅ Voicemail drop sent to {to_number}")
        except Exception as e:
            logger.error(f"   ❌ Failed to redirect to voicemail drop: {e}")
        call_status_map.pop(call_sid, None)
        return "", 204

    elif answered_by == "machine_start":
        logger.info(f"🤖 Machine start: {to_number} — waiting for beep callback")
        if call_sid in call_status_map:
            call_status_map[call_sid]["status"] = "machine_start"
        return "", 204

    elif answered_by == "fax":
        # Fax — hang up cleanly
        logger.info(f"📠 Fax detected: {to_number} — hanging up")
        try:
            client.calls(call_sid).update(status="completed")
        except Exception:
            pass
        call_status_map.pop(call_sid, None)
        return "", 204

    else:
        # unknown — WebSocket stream is already connected, do nothing
        logger.info(f"❓ AMD unknown for {to_number} — leaving call connected (Arcadio is streaming)")
        if call_sid in call_status_map:
            call_status_map[call_sid]["status"] = "unknown"
        return "", 204


@app.route("/call-status", methods=["POST"])
def call_status():
    call_sid = request.form.get("CallSid", "unknown")
    status   = request.form.get("CallStatus", "unknown")
    to       = request.form.get("To", "unknown")
    duration = request.form.get("CallDuration", "0")
    logger.info(f"📋 Call complete — SID: {call_sid}, To: {to}, Status: {status}, Duration: {duration}s")
    call_status_map.pop(call_sid, None)
    return "", 204


# ─── MAIN ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
