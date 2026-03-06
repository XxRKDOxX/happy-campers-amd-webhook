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
import json
import logging
import requests as req
from flask import Flask, request, Response
from twilio.rest import Client as TwilioClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ─── CONFIG (set as Railway environment variables) ────────────────────────────

TWILIO_ACCOUNT_SID  = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN   = os.environ.get("TWILIO_AUTH_TOKEN")
FROM_NUMBER         = os.environ.get("FROM_NUMBER", "+13528978771")
ELEVENLABS_API_KEY  = os.environ.get("ELEVENLABS_API_KEY")
ELEVENLABS_AGENT_ID         = os.environ.get("ELEVENLABS_AGENT_ID")          # Arcadio — guest outreach
ELEVENLABS_GENERAL_AGENT_ID = os.environ.get("ELEVENLABS_GENERAL_AGENT_ID")  # General purpose — follows call_reason
ELEVENLABS_INBOUND_AGENT_ID  = os.environ.get("ELEVENLABS_INBOUND_AGENT_ID")   # Voicemail Assistant — answers inbound calls
ELEVENLABS_PHONE_ID         = os.environ.get("ELEVENLABS_PHONE_ID", "phnum_4501kjx114q0f8j8cn0c3tt3b0f3")
VOICEMAIL_AUDIO_URL = os.environ.get("VOICEMAIL_AUDIO_URL")
OWNER_CELL_NUMBER   = os.environ.get("OWNER_CELL_NUMBER", "+13528970290")    # Primary owner number (Google Voice)
OWNER_CELL_NUMBER2  = os.environ.get("OWNER_CELL_NUMBER2", "+14074569616")   # Secondary owner number (real cell)
OWNER_RING_TIMEOUT  = int(os.environ.get("OWNER_RING_TIMEOUT", "10"))        # Seconds to ring owner before Arcadio picks up
RAILWAY_PUBLIC_URL  = os.environ.get("RAILWAY_PUBLIC_URL", "")               # e.g. https://web-production-c7ecb.up.railway.app
OPENAI_API_KEY      = os.environ.get("OPENAI_API_KEY")                        # For summarizing transcripts
SMS_RECAP_TO        = os.environ.get("SMS_RECAP_TO", "+13528970290")          # Number to text call recaps to
SMS_RECAP_FROM      = os.environ.get("SMS_RECAP_FROM", "+13528978771")        # Twilio number to send SMS from

# In-memory store: CallSid -> status ("human" | "machine" | "pending")
call_status_map = {}

# Track inbound call SIDs so we know to send a recap SMS when they end
inbound_call_sids = set()

# ─── ROUTES ───────────────────────────────────────────────────────────────────

@app.route("/inbound", methods=["POST"])
def inbound():
    """
    Twilio calls this when someone calls the business number (352) 897-8771.
    Rings the owner's cell first for OWNER_RING_TIMEOUT seconds.
    If unanswered, /inbound-fallback fires and Arcadio picks up.
    """
    call_sid    = request.form.get("CallSid", "unknown")
    from_number = request.form.get("From", "unknown")
    to_number   = request.form.get("To", FROM_NUMBER)

    logger.info(f"📲 Inbound call — SID: {call_sid}, From: {from_number}")

    base_url = RAILWAY_PUBLIC_URL.rstrip("/") or "https://web-production-c7ecb.up.railway.app"
    fallback_url = f"{base_url}/inbound-fallback"
    status_callback_url = f"{base_url}/call-status"

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Dial timeout="{OWNER_RING_TIMEOUT}" action="{fallback_url}" method="POST">
        <Number statusCallback="{status_callback_url}" statusCallbackEvent="completed">{OWNER_CELL_NUMBER}</Number>
    </Dial>
</Response>"""
    return Response(twiml, mimetype="text/xml")


@app.route("/inbound-fallback", methods=["POST"])
def inbound_fallback():
    """
    Fires when owner doesn't answer the inbound call within OWNER_RING_TIMEOUT seconds.
    Connects the caller to Arcadio via ElevenLabs.
    """
    call_sid    = request.form.get("CallSid", "unknown")
    from_number = request.form.get("From", "unknown")
    dial_status = request.form.get("DialCallStatus", "no-answer")

    logger.info(f"🤖 Inbound fallback — SID: {call_sid}, From: {from_number}, DialStatus: {dial_status}")

    # Always connect to Voicemail Assistant (inbound agent) regardless of DialCallStatus.
    # 'completed' can mean voicemail answered, not necessarily the owner.
    agent_id = ELEVENLABS_INBOUND_AGENT_ID or ELEVENLABS_GENERAL_AGENT_ID or ELEVENLABS_AGENT_ID
    logger.info(f"   🤖 Connecting to Voicemail Assistant ({agent_id}) for inbound caller {from_number}")
    inbound_call_sids.add(call_sid)  # Track so we send a recap SMS when call ends

    try:
        response = req.post(
            "https://api.elevenlabs.io/v1/convai/twilio/register-call",
            headers={
                "xi-api-key": ELEVENLABS_API_KEY,
                "Content-Type": "application/json"
            },
            json={
                "agent_id": agent_id,
                "call_sid": call_sid,
                "direction": "inbound",
                "from_number": from_number,
                "to_number": FROM_NUMBER,
                "conversation_initiation_client_data": {
                    "dynamic_variables": {
                        "call_reason": "standard guest outreach"
                    }
                }
            },
            timeout=10
        )

        if response.status_code == 200:
            content_type = response.headers.get("Content-Type", "")
            twiml_response = None
            if "xml" in content_type or response.text.strip().startswith("<"):
                logger.info(f"   ✅ Got TwiML XML from ElevenLabs for inbound {call_sid}")
                twiml_response = response.text
            else:
                try:
                    data = response.json()
                    twiml_response = data.get("twiml") or data.get("twiML") or data.get("TwiML")
                    if twiml_response:
                        logger.info(f"   ✅ Got TwiML from JSON for inbound {call_sid}")
                except Exception:
                    pass

            if twiml_response:
                # Launch background thread to fetch transcript and send recap SMS after call ends
                import threading
                threading.Thread(
                    target=send_recap_sms_delayed,
                    args=(call_sid, from_number),
                    daemon=True
                ).start()
                logger.info(f"   🕐 Recap SMS thread started for inbound call {call_sid}")
                return Response(twiml_response, mimetype="text/xml")

        logger.error(f"   ❌ ElevenLabs register-call failed: {response.status_code} — {response.text[:200]}")

    except Exception as e:
        logger.error(f"   ❌ ElevenLabs register-call exception: {e}")

    # Fallback — play a message if Arcadio can't connect
    fallback_twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>Thanks for calling Happy Campers Rescue Ranch. We're unable to take your call right now. Please try again later.</Say>
    <Hangup/>
</Response>"""
    return Response(fallback_twiml, mimetype="text/xml")


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
    call_sid    = request.form.get("CallSid", "unknown")
    to_number   = request.form.get("To", "")
    from_number = request.form.get("From", "")
    call_reason = request.args.get("call_reason") or request.form.get("call_reason", "")

    logger.info(f"📞 Call answered — SID: {call_sid}, To: {to_number}, Reason: {call_reason or 'none'}")

    # Mark as pending AMD result
    call_status_map[call_sid] = {"to": to_number, "status": "pending", "call_reason": call_reason}

    # Route to general-purpose agent if call_reason is provided, otherwise use Arcadio
    use_general = bool(call_reason and call_reason.strip() and call_reason.strip().lower() != "standard guest outreach")
    agent_id = (ELEVENLABS_GENERAL_AGENT_ID if use_general else ELEVENLABS_AGENT_ID) or ELEVENLABS_AGENT_ID
    logger.info(f"   🤖 Using agent: {'General Purpose' if use_general else 'Arcadio'} ({agent_id})")

    # Call ElevenLabs register-call to get TwiML for WebSocket stream
    try:
        response = req.post(
            "https://api.elevenlabs.io/v1/convai/twilio/register-call",
            headers={
                "xi-api-key": ELEVENLABS_API_KEY,
                "Content-Type": "application/json"
            },
            json={
                "agent_id": agent_id,
                "call_sid": call_sid,
                "direction": "outbound",
                "from_number": FROM_NUMBER,
                "to_number": to_number,
                "conversation_initiation_client_data": {
                    "dynamic_variables": {
                        "call_reason": call_reason if call_reason else "standard guest outreach"
                    }
                }
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


@app.route("/call-transcript", methods=["POST"])
def call_transcript():
    """
    ElevenLabs post-call webhook — fires after a Voicemail Assistant call ends.
    Receives the full conversation transcript, summarizes it with GPT, and texts
    a recap to the owner at SMS_RECAP_TO.
    """
    try:
        data = request.get_json(force=True) or {}
        logger.info(f"📝 Transcript webhook received — keys: {list(data.keys())}")

        # Extract transcript turns
        transcript_turns = []
        conversation = data.get("conversation", {}) or data.get("data", {}) or data
        messages = (
            conversation.get("transcript") or
            conversation.get("messages") or
            data.get("transcript") or
            data.get("messages") or []
        )

        for msg in messages:
            role = msg.get("role", "unknown").capitalize()
            text = msg.get("message") or msg.get("content") or msg.get("text") or ""
            if text.strip():
                transcript_turns.append(f"{role}: {text.strip()}")

        # Caller phone number
        caller_number = (
            data.get("from_number") or
            data.get("caller") or
            conversation.get("from_number") or
            "Unknown"
        )

        # Call duration
        duration_sec = (
            data.get("duration") or
            conversation.get("duration") or
            data.get("call_duration") or 0
        )
        if duration_sec:
            mins, secs = divmod(int(duration_sec), 60)
            duration_str = f"{mins}m {secs}s" if mins else f"{secs}s"
        else:
            duration_str = "unknown"

        if not transcript_turns:
            logger.info("   ⚠️  No transcript content found — skipping SMS")
            return "", 204

        full_transcript = "\n".join(transcript_turns)
        logger.info(f"   📋 Transcript ({len(transcript_turns)} turns) from {caller_number}")

        # Summarize with GPT
        summary = summarize_transcript(full_transcript, caller_number)

        # Build SMS message
        sms_body = (
            f"📞 Voicemail Assistant recap\n"
            f"From: {caller_number}\n"
            f"Duration: {duration_str}\n\n"
            f"{summary}"
        )

        # Send SMS via Twilio
        twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        message = twilio_client.messages.create(
            body=sms_body[:1600],  # SMS limit
            from_=SMS_RECAP_FROM,
            to=SMS_RECAP_TO
        )
        logger.info(f"   ✅ Recap SMS sent — SID: {message.sid}")

    except Exception as e:
        logger.error(f"   ❌ Transcript webhook error: {e}")

    return "", 204


def summarize_transcript(transcript: str, caller_number: str) -> str:
    """Use OpenAI to summarize a call transcript into a short recap."""
    if not OPENAI_API_KEY:
        # Fallback: return first 800 chars of raw transcript
        return transcript[:800]

    try:
        response = req.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "gpt-4.1-mini",
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are a helpful assistant that summarizes phone call transcripts into "
                            "brief, clear recaps for a business owner. Be concise — 3 to 5 sentences max. "
                            "Focus on: who called, what they wanted, any key details, and whether a callback is needed."
                        )
                    },
                    {
                        "role": "user",
                        "content": f"Summarize this call transcript:\n\n{transcript[:3000]}"
                    }
                ],
                "max_tokens": 200,
                "temperature": 0.3
            },
            timeout=15
        )
        if response.status_code == 200:
            return response.json()["choices"][0]["message"]["content"].strip()
        else:
            logger.error(f"OpenAI error: {response.status_code} — {response.text[:200]}")
            return transcript[:800]
    except Exception as e:
        logger.error(f"OpenAI summarize error: {e}")
        return transcript[:800]


@app.route("/call-status", methods=["POST"])
def call_status():
    import threading
    call_sid = request.form.get("CallSid", "unknown")
    status   = request.form.get("CallStatus", "unknown")
    to       = request.form.get("To", "unknown")
    duration = request.form.get("CallDuration", "0")
    from_num = request.form.get("From", "unknown")
    logger.info(f"📋 Call complete — SID: {call_sid}, To: {to}, Status: {status}, Duration: {duration}s")
    call_status_map.pop(call_sid, None)

    # If this was an inbound call handled by the Voicemail Assistant, send a recap SMS
    if call_sid in inbound_call_sids:
        inbound_call_sids.discard(call_sid)
        logger.info(f"📝 Inbound call ended — fetching transcript for recap SMS")
        # Run in background thread so we don't block Twilio's status callback
        threading.Thread(
            target=send_recap_sms,
            args=(call_sid, from_num, int(duration)),
            daemon=True
        ).start()

    return "", 204


def send_recap_sms_delayed(call_sid: str, from_number: str):
    """Wait for the inbound call to complete by polling Twilio, then send recap SMS."""
    import time
    max_wait = 600  # max 10 minutes
    poll_interval = 15  # check every 15 seconds
    waited = 0

    try:
        twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        logger.info(f"   ⏳ Waiting for call {call_sid} to complete...")

        while waited < max_wait:
            time.sleep(poll_interval)
            waited += poll_interval
            try:
                call = twilio_client.calls(call_sid).fetch()
                logger.info(f"   📊 Call {call_sid} status: {call.status} (waited {waited}s)")
                if call.status in ("completed", "failed", "busy", "no-answer", "canceled"):
                    duration_sec = int(call.duration or 0)
                    send_recap_sms(call_sid, from_number, duration_sec)
                    return
            except Exception as e:
                logger.warning(f"   ⚠️ Could not poll call status: {e}")
                break

        logger.warning(f"   ⚠️ Timed out waiting for call {call_sid} to complete")
    except Exception as e:
        logger.error(f"   ❌ send_recap_sms_delayed error: {e}")


def send_recap_sms(call_sid: str, from_number: str, duration_sec: int):
    """Fetch the ElevenLabs conversation summary and SMS a recap to the owner."""
    import time
    # Wait for ElevenLabs to finalize the transcript summary
    time.sleep(10)

    try:
        headers = {"xi-api-key": ELEVENLABS_API_KEY}
        agent_id = ELEVENLABS_INBOUND_AGENT_ID

        # Get the most recent conversation for the Voicemail Assistant agent
        r = req.get(
            "https://api.elevenlabs.io/v1/convai/conversations",
            headers=headers,
            params={"agent_id": agent_id, "page_size": 1},
            timeout=15
        )
        if r.status_code != 200:
            logger.error(f"   ❌ Failed to fetch conversations: {r.status_code}")
            return

        convs = r.json().get("conversations", [])
        if not convs:
            logger.warning("   ⚠️ No conversations found for recap")
            return

        conv = convs[0]
        conv_id = conv["conversation_id"]
        title = conv.get("call_summary_title", "")
        caller = conv.get("user_id") or from_number
        duration_secs = conv.get("call_duration_secs") or duration_sec
        mins, secs = divmod(int(duration_secs), 60)
        duration_str = f"{mins}m {secs}s" if mins else f"{secs}s"

        logger.info(f"   📋 Got conversation {conv_id}: '{title}'")

        # Fetch full detail to get transcript_summary
        r2 = req.get(
            f"https://api.elevenlabs.io/v1/convai/conversations/{conv_id}",
            headers=headers,
            timeout=15
        )
        summary = ""
        if r2.status_code == 200:
            analysis = r2.json().get("analysis", {})
            summary = analysis.get("transcript_summary") or ""

        if not summary:
            logger.warning("   ⚠️ No summary yet — skipping SMS")
            return

        # Build and send SMS
        sms_body = (
            f"📞 {title}\n"
            f"From: {caller}\n"
            f"Duration: {duration_str}\n\n"
            f"{summary.strip()}"
        )

        twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        message = twilio_client.messages.create(
            body=sms_body[:1600],
            from_=SMS_RECAP_FROM,
            to=SMS_RECAP_TO
        )
        logger.info(f"   ✅ Recap SMS sent — SID: {message.sid}")

    except Exception as e:
        logger.error(f"   ❌ send_recap_sms error: {e}")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
