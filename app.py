#!/usr/bin/env python3
"""
Happy Campers Rescue Ranch — AMD Voicemail Webhook Server
Production deployment for Railway.

Flow:
  1. Twilio dials the guest with AMD enabled
  2. /answer fires when call connects → stores the To number, plays silence while AMD analyzes
  3. /amd-callback fires with the result:
     - human            → ElevenLabs outbound API initiates a fresh agent call to the stored number
     - machine_end_beep → pre-recorded voicemail drop plays
     - machine_start    → wait for machine_end_* callback
     - fax/unknown      → hang up

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

# In-memory store: CallSid -> To number (cleared after AMD callback)
call_numbers = {}

# ─── ROUTES ───────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok", "service": "Happy Campers AMD Voicemail Webhook"}, 200


@app.route("/answer", methods=["POST"])
def answer():
    """
    Twilio calls this when the call is answered.
    Stores the To number keyed by CallSid, then plays silence while AMD runs.
    """
    call_sid  = request.form.get("CallSid", "unknown")
    to_number = request.form.get("To", "")

    # Store the To number so AMD callback can use it
    if call_sid and to_number:
        call_numbers[call_sid] = to_number

    logger.info(f"📞 Call answered — SID: {call_sid}, To: {to_number} — holding for AMD...")

    # Play silence while AMD determines human vs machine (8 seconds max)
    twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Pause length="8"/>
    <Hangup/>
</Response>"""
    return Response(twiml, mimetype="text/xml")


@app.route("/amd-callback", methods=["POST"])
def amd_callback():
    """
    Twilio AMD result callback — fires asynchronously after AMD analysis.
    """
    from twilio.rest import Client

    answered_by = request.form.get("AnsweredBy", "unknown")
    call_sid    = request.form.get("CallSid", "unknown")

    # Retrieve the stored To number for this call
    to_number = call_numbers.pop(call_sid, None) or request.form.get("To", "")

    logger.info(f"🔍 AMD — SID: {call_sid}, To: {to_number}, AnsweredBy: {answered_by}")

    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

    if answered_by == "human":
        if not to_number:
            logger.error(f"❌ Human detected but no To number found for SID: {call_sid}")
            return "", 204

        logger.info(f"✅ Human answered: {to_number} — initiating ElevenLabs agent call")
        try:
            # Hang up the AMD detection call first
            try:
                client.calls(call_sid).update(status="completed")
            except Exception:
                pass

            # Use ElevenLabs outbound call API to call them back with the agent
            response = req.post(
                "https://api.elevenlabs.io/v1/convai/twilio/outbound-call",
                headers={
                    "xi-api-key": ELEVENLABS_API_KEY,
                    "Content-Type": "application/json"
                },
                json={
                    "agent_id": ELEVENLABS_AGENT_ID,
                    "agent_phone_number_id": ELEVENLABS_PHONE_ID,
                    "to_number": to_number
                },
                timeout=15
            )
            if response.status_code == 200:
                logger.info(f"   ✅ ElevenLabs agent call initiated to {to_number}")
            else:
                logger.error(f"   ❌ ElevenLabs call failed: {response.status_code} — {response.text}")
        except Exception as e:
            logger.error(f"   ❌ Exception: {e}")
        return "", 204

    elif answered_by in ("machine_end_beep", "machine_end_silence", "machine_end_other"):
        logger.info(f"📬 Voicemail detected: {to_number} — playing voicemail drop")
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
        return "", 204

    elif answered_by == "machine_start":
        logger.info(f"🤖 Machine start: {to_number} — waiting for beep callback")
        # Keep the number stored for the follow-up machine_end_* callback
        if to_number and call_sid:
            call_numbers[call_sid] = to_number
        return "", 204

    else:
        # Fax or unknown — hang up cleanly
        logger.info(f"❓ {answered_by}: {to_number} — hanging up")
        try:
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
    # Clean up any leftover stored numbers
    call_numbers.pop(call_sid, None)
    return "", 204


# ─── MAIN ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
