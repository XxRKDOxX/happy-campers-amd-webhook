#!/usr/bin/env python3
"""
Happy Campers Rescue Ranch — AMD Voicemail Webhook Server
Production deployment for Railway.
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
VOICEMAIL_AUDIO_URL = os.environ.get("VOICEMAIL_AUDIO_URL")

# ─── ROUTES ───────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok", "service": "Happy Campers AMD Voicemail Webhook"}, 200


@app.route("/answer", methods=["POST"])
def answer():
    """
    Twilio calls this when the call is answered.
    Registers the call with ElevenLabs and returns TwiML to connect the agent.
    AMD runs asynchronously in parallel.
    """
    call_sid    = request.form.get("CallSid", "unknown")
    to_number   = request.form.get("To", "unknown")
    from_number = request.form.get("From", FROM_NUMBER)

    logger.info(f"📞 Call answered — SID: {call_sid}, To: {to_number}")

    try:
        response = req.post(
            "https://api.elevenlabs.io/v1/convai/twilio/register-call",
            headers={
                "xi-api-key": ELEVENLABS_API_KEY,
                "Content-Type": "application/json"
            },
            json={
                "agent_id": ELEVENLABS_AGENT_ID,
                "from_number": from_number,
                "to_number": to_number,
                "direction": "outbound"
            },
            timeout=10
        )

        if response.status_code == 200:
            logger.info(f"   ✅ ElevenLabs agent connected for {to_number}")
            return Response(response.text, mimetype="text/xml")
        else:
            logger.error(f"   ❌ ElevenLabs error: {response.status_code} {response.text}")

    except Exception as e:
        logger.error(f"   ❌ Exception: {e}")

    # Fallback
    return Response("""<?xml version="1.0" encoding="UTF-8"?>
<Response><Hangup/></Response>""", mimetype="text/xml")


@app.route("/amd-callback", methods=["POST"])
def amd_callback():
    """
    Twilio AMD result callback.
    If voicemail detected, redirect the call to play the pre-recorded drop.
    """
    from twilio.rest import Client

    answered_by = request.form.get("AnsweredBy", "unknown")
    call_sid    = request.form.get("CallSid", "unknown")
    to_number   = request.form.get("To", "unknown")

    logger.info(f"🔍 AMD — SID: {call_sid}, To: {to_number}, AnsweredBy: {answered_by}")

    if answered_by == "human":
        logger.info(f"✅ Human answered: {to_number}")
        return "", 204

    elif answered_by in ("machine_end_beep", "machine_end_silence", "machine_end_other"):
        logger.info(f"📬 Voicemail detected: {to_number} — playing voicemail drop")
        try:
            client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            client.calls(call_sid).update(
                twiml=f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Play>{VOICEMAIL_AUDIO_URL}</Play>
    <Hangup/>
</Response>"""
            )
            logger.info(f"   ✅ Voicemail drop sent to {to_number}")
        except Exception as e:
            logger.error(f"   ❌ Failed to redirect: {e}")
        return "", 204

    elif answered_by == "machine_start":
        logger.info(f"🤖 Machine start detected: {to_number} — waiting for beep")
        return "", 204

    else:
        logger.info(f"❓ {answered_by}: {to_number} — hanging up")
        try:
            client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            client.calls(call_sid).update(status="completed")
        except Exception:
            pass
        return "", 204


@app.route("/call-status", methods=["POST"])
def call_status():
    call_sid = request.form.get("CallSid", "unknown")
    status   = request.form.get("CallStatus", "unknown")
    to       = request.form.get("To", "unknown")
    duration = request.form.get("CallDuration", "0")
    logger.info(f"📋 Call complete — SID: {call_sid}, To: {to}, Status: {status}, Duration: {duration}s")
    return "", 204


# ─── MAIN ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
