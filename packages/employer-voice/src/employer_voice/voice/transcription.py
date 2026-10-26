"""OpenAI Whisper STT wrapper.

Takes a WAV blob + model name and returns the transcribed text. Any API
failure propagates as a ``VoiceIOError`` so callers have one exception type
to catch across the voice pipeline.
"""
from __future__ import annotations

import io

from employer_voice.voice import VoiceIOError


async def transcribe_wav(
    wav_bytes: bytes,
    *,
    api_key: str,
    model: str = "whisper-1",
) -> str:
    if not wav_bytes:
        return ""
    if not api_key:
        raise VoiceIOError("OpenAI API key is empty; set it in the configured env var")

    try:
        from openai import AsyncOpenAI
    except Exception as exc:
        raise VoiceIOError("OpenAI SDK not installed; add 'openai' to your deps") from exc

    client = AsyncOpenAI(api_key=api_key)
    try:
        file_tuple = ("speech.wav", io.BytesIO(wav_bytes), "audio/wav")
        resp = await client.audio.transcriptions.create(
            model=model,
            file=file_tuple,
        )
    except Exception as exc:
        raise VoiceIOError(f"Transcription failed: {exc}") from exc
    finally:
        close = getattr(client, "close", None)
        if close is not None:
            try:
                await close()
            except Exception:
                pass

    text = getattr(resp, "text", None)
    if text is None and isinstance(resp, dict):
        text = resp.get("text", "")
    return (text or "").strip()
