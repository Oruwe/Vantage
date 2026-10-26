"""OpenAI TTS wrapper.

Takes text + voice/model and returns MP3 bytes. The caller is responsible for
playback (see ``speaker.play_audio_bytes``).
"""
from __future__ import annotations

from employer_voice.voice import VoiceIOError


async def synthesize(
    text: str,
    *,
    api_key: str,
    model: str = "tts-1",
    voice: str = "alloy",
) -> bytes:
    if not text.strip():
        return b""
    if not api_key:
        raise VoiceIOError("OpenAI API key is empty; set it in the configured env var")

    try:
        from openai import AsyncOpenAI
    except Exception as exc:
        raise VoiceIOError("OpenAI SDK not installed; add 'openai' to your deps") from exc

    client = AsyncOpenAI(api_key=api_key)
    try:
        resp = await client.audio.speech.create(
            model=model,
            voice=voice,
            input=text,
            response_format="mp3",
        )
    except Exception as exc:
        raise VoiceIOError(f"TTS synthesis failed: {exc}") from exc

    try:
        if hasattr(resp, "aread"):
            data = await resp.aread()
        elif hasattr(resp, "read"):
            data = resp.read()
        elif hasattr(resp, "content"):
            data = resp.content
        else:
            data = bytes(resp)
    finally:
        close = getattr(client, "close", None)
        if close is not None:
            try:
                await close()
            except Exception:
                pass

    return data if isinstance(data, (bytes, bytearray)) else bytes(data or b"")
