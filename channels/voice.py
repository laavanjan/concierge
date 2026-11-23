"""Voice channel.

* Browser voice (demo): the widget streams speech-to-text with the Web Speech API (or uploads audio for
  server-side Whisper) and speaks replies with speechSynthesis.
* Server STT: faster-whisper (`pip install .[voice]`, STT_PROVIDER=faster-whisper).
* Phone: Twilio Voice webhooks with <Gather input="speech"> and <Say>.
Replies are shortened for speech by the agents (channel=voice) and by `for_voice`.
"""

from __future__ import annotations

import asyncio
import io
import logging
from functools import lru_cache
from xml.sax.saxutils import escape

from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel

from channels.formatting import for_voice
from common.config import get_settings
from common.domain import domain
from common.messages import InboundMessage

log = logging.getLogger(__name__)
router = APIRouter(tags=["voice"])


@lru_cache(maxsize=1)
def _whisper():
    from faster_whisper import WhisperModel

    return WhisperModel(get_settings().whisper_model, device="cpu", compute_type="int8")


def transcribe(audio: bytes) -> str:
    segments, _info = _whisper().transcribe(io.BytesIO(audio), beam_size=1, vad_filter=True)
    return " ".join(s.text.strip() for s in segments).strip()


class VoiceText(BaseModel):
    transcript: str
    session_id: str | None = None
    caller: str = "browser"


async def _reply(request: Request, text: str, session_id: str | None, address: str) -> dict:
    out = await request.app.state.runtime.handle(
        InboundMessage(channel="voice", address=address, text=text, session_id=session_id)
    )
    return {
        "session_id": out.session_id,
        "transcript": text,
        "reply": out.text,
        "speech": for_voice(out),
        "status": out.status,
        "citations": out.citations,
        "latency_ms": out.latency_ms,
    }


@router.post("/v1/voice/text")
async def voice_text(request: Request, body: VoiceText) -> dict:
    """Browser did speech-to-text; we return text to speak."""
    if not body.transcript.strip():
        raise HTTPException(status_code=422, detail="empty transcript")
    return await _reply(request, body.transcript.strip(), body.session_id, body.caller)


@router.post("/v1/voice/audio")
async def voice_audio(request: Request, audio: UploadFile = File(...), session_id: str | None = Form(default=None)) -> dict:
    """Server-side speech-to-text with Whisper."""
    if get_settings().stt_provider != "faster-whisper":
        raise HTTPException(status_code=501, detail="server STT disabled; set STT_PROVIDER=faster-whisper or use browser STT")
    data = await audio.read()
    text = await asyncio.to_thread(transcribe, data)
    if not text:
        return {"session_id": session_id, "transcript": "", "reply": "Sorry, I didn't catch that.", "speech": "Sorry, I didn't catch that."}
    return await _reply(request, text, session_id, "browser")


def _gather(say: str) -> Response:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        f'<Gather input="speech" action="/channels/voice/twilio/gather" speechTimeout="auto" language="en-US">'
        f"<Say>{escape(say)}</Say></Gather><Say>Goodbye.</Say></Response>"
    )
    return Response(content=xml, media_type="application/xml")


@router.post("/channels/voice/twilio")
async def twilio_voice_start() -> Response:
    return _gather(f"Hello, you've reached {domain().property.name}. How can I help with your stay?")


@router.post("/channels/voice/twilio/gather")
async def twilio_voice_gather(request: Request) -> Response:
    form = await request.form()
    speech = str(form.get("SpeechResult", "")).strip()
    caller = str(form.get("From", "unknown"))
    if not speech:
        return _gather("Sorry, I didn't catch that. Could you say it again?")
    out = await request.app.state.runtime.handle(InboundMessage(channel="voice", address=caller, text=speech))
    return _gather(for_voice(out) + " Is there anything else?")
