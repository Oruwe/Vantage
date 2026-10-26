"""Speaker playback for TTS audio.

Accepts the MP3 bytes OpenAI returns, decodes them to PCM via ``soundfile``,
and plays through the default output device. Decoder + player are both
soft-imported so the rest of the package stays usable without audio deps.
"""
from __future__ import annotations

import io

from employer_voice.voice import VoiceIOError


def _import_audio_stack():
    try:
        import numpy as np
        import sounddevice as sd
        import soundfile as sf
    except Exception as exc:
        raise VoiceIOError(
            "Audio playback requires 'sounddevice', 'soundfile', and 'numpy'. "
            "Install with: pip install sounddevice soundfile numpy"
        ) from exc
    return np, sd, sf


def play_audio_bytes(audio_bytes: bytes) -> None:
    if not audio_bytes:
        return
    np, sd, sf = _import_audio_stack()
    try:
        data, sr = sf.read(io.BytesIO(audio_bytes), dtype="float32", always_2d=False)
        sd.play(data, sr)
        sd.wait()
    except Exception as exc:  # pragma: no cover — depends on local hardware
        raise VoiceIOError(f"Audio playback failed: {exc}") from exc
