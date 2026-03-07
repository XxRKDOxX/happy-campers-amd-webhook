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
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
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
EMAIL_RECAP_TO      = os.environ.get("EMAIL_RECAP_TO", "happycampersrescueranch@gmail.com")  # Email to send call recaps to
GMAIL_USER          = os.environ.get("GMAIL_USER", "happycampersrescueranch@gmail.com")       # Gmail address to send from
GMAIL_APP_PASSWORD  = os.environ.get("GMAIL_APP_PASSWORD", "")                               # Gmail App Password (not account password)

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
    ElevenLabs post-call webhook - fires after every conversation ends.
    Payload: { "type": "post_call_transcription", "data": { ... } }
    Sends an email recap to the owner.
    """
    try:
        data = request.get_json(force=True) or {}
        event_type = data.get("type", "")
        conv_data = data.get("data", {}) or {}
        logger.info(f"\U0001f4dd ElevenLabs webhook received - type: {event_type}")

        # Only process transcription events
        if event_type != "post_call_transcription":
            logger.info(f"   \u2139\ufe0f  Skipping event type: {event_type}")
            return "", 204

        conv_id = conv_data.get("conversation_id", "unknown")
        agent_id = conv_data.get("agent_id", "")
        logger.info(f"   \U0001f4cb Conversation ID: {conv_id}, Agent: {agent_id}")

        # Only send recap for Voicemail Assistant calls
        if agent_id != ELEVENLABS_INBOUND_AGENT_ID:
            logger.info(f"   \u2139\ufe0f  Not a Voicemail Assistant call - skipping recap")
            return "", 204

        # Extract caller number from metadata
        metadata = conv_data.get("metadata") or {}
        caller_number = "Unknown"
        if isinstance(metadata, dict):
            twilio_meta = metadata.get("twilio") or {}
            caller_number = twilio_meta.get("from") or metadata.get("from_number") or "Unknown"

        # Duration
        duration_sec = conv_data.get("call_duration_secs") or 0
        mins, secs = divmod(int(duration_sec), 60)
        duration_str = f"{mins}m {secs}s" if mins else f"{secs}s"

        # Summary from ElevenLabs analysis
        analysis = conv_data.get("analysis") or {}
        summary = analysis.get("transcript_summary") or ""
        title = analysis.get("call_summary_title") or "Voicemail Assistant Call"

        # Fallback: build from raw transcript turns
        if not summary:
            turns = []
            for msg in (conv_data.get("transcript") or []):
                role = msg.get("role", "unknown").capitalize()
                text = msg.get("message") or ""
                if text.strip():
                    turns.append(f"{role}: {text.strip()}")
            summary = "\n".join(turns[:20]) if turns else "(no transcript available)"

        audio_url = f"https://elevenlabs.io/app/conversational-ai/history/{conv_id}"
        subject = f"\U0001f4de Call Recap: {title}"

        body_text = (
            f"Call Recap - Voicemail Assistant\n"
            f"From: {caller_number}\n"
            f"Duration: {duration_str}\n\n"
            f"Summary:\n{summary.strip()}\n\n"
            f"Listen to recording:\n{audio_url}"
        )
        body_html = f"""
        <html><body style='font-family:Arial,sans-serif;font-size:14px'>
        <h2>\U0001f4de Call Recap - Voicemail Assistant</h2>
        <table>
          <tr><td><b>From:</b></td><td>{caller_number}</td></tr>
          <tr><td><b>Duration:</b></td><td>{duration_str}</td></tr>
        </table>
        <h3>Summary</h3>
        <p>{summary.strip().replace(chr(10), '<br>')}</p>
        <p><a href='{audio_url}'>&#9654; Listen to call recording on ElevenLabs</a></p>
        </body></html>
        """

        email_msg = MIMEMultipart("alternative")
        email_msg["Subject"] = subject
        email_msg["From"] = GMAIL_USER
        email_msg["To"] = EMAIL_RECAP_TO
        email_msg.attach(MIMEText(body_text, "plain"))
        email_msg.attach(MIMEText(body_html, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            smtp.sendmail(GMAIL_USER, EMAIL_RECAP_TO, email_msg.as_string())

        logger.info(f"   \u2705 Recap email sent to {EMAIL_RECAP_TO} - {conv_id}")
    except Exception as e:
        logger.error(f"   \u274c Transcript webhook error: {e}")
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
    """Wait a fixed time after the Voicemail Assistant connects, then fetch the latest
    ElevenLabs conversation and send an email recap. Simple and reliable."""
    import time
    # Wait 3 minutes — enough for most calls to finish and ElevenLabs to save the transcript
    wait_seconds = 180
    logger.info(f"   ⏳ Will send recap email in {wait_seconds}s for call from {from_number}")
    time.sleep(wait_seconds)
    logger.info(f"   ⏰ Wait complete — fetching transcript for {from_number}")
    send_recap_sms(call_sid, from_number, 0)


def send_recap_sms(call_sid: str, from_number: str, duration_sec: int):
    """Fetch the ElevenLabs conversation summary and send an email recap to the owner."""
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
        title = conv.get("call_summary_title", "Voicemail Assistant Call")
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
            logger.warning("   ⚠️ No summary yet — skipping email")
            return

        # Build email
        audio_url = f"https://elevenlabs.io/app/conversational-ai/history/{conv_id}"
        subject = f"📞 Call Recap: {title}"
        body_text = (
            f"Call Recap — Voicemail Assistant\n"
            f"From: {caller}\n"
            f"Duration: {duration_str}\n\n"
            f"Summary:\n{summary.strip()}\n\n"
            f"Listen to recording:\n{audio_url}"
        )
        body_html = f"""
        <html><body>
        <h2>📞 Call Recap — Voicemail Assistant</h2>
        <table style='font-family:Arial,sans-serif;font-size:14px'>
          <tr><td><b>From:</b></td><td>{caller}</td></tr>
          <tr><td><b>Duration:</b></td><td>{duration_str}</td></tr>
        </table>
        <h3>Summary</h3>
        <p>{summary.strip().replace(chr(10), '<br>')}</p>
        <h3>Recording</h3>
        <p><a href='{audio_url}'>▶ Listen to call recording on ElevenLabs</a></p>
        </body></html>
        """

        # Send via Gmail SMTP
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = GMAIL_USER
        msg["To"] = EMAIL_RECAP_TO
        msg.attach(MIMEText(body_text, "plain"))
        msg.attach(MIMEText(body_html, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            smtp.sendmail(GMAIL_USER, EMAIL_RECAP_TO, msg.as_string())

        logger.info(f"   ✅ Recap email sent to {EMAIL_RECAP_TO} for conversation {conv_id}")

    except Exception as e:
        logger.error(f"   ❌ send_recap_sms error: {e}")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
