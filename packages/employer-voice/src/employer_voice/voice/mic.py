"""Microphone capture with silence-based auto-cutoff.

Records 16-bit PCM mono audio from the default input device, stops when the
rolling RMS drops below ``silence_threshold`` for ``silence_seconds``, and
returns a WAV blob suitable for upload to the Whisper endpoint.

``sounddevice`` and ``numpy`` are soft-imported — if either is missing the
caller gets a ``VoiceIOError`` with an install hint rather than an import error.
"""
from __future__ import annotations

import io
import wave
from dataclasses import dataclass

from employer_voice.voice import VoiceIOError


@dataclass
class RecordingResult:
    wav_bytes: bytes
    duration_seconds: float
    sample_rate: int


def _import_audio_stack():
    try:
        import numpy as np
        import sounddevice as sd
    except Exception as exc:
        raise VoiceIOError(
            "Microphone capture requires 'sounddevice' and 'numpy'. "
            "Install with: pip install sounddevice numpy"
        ) from exc
    return np, sd


def record_until_silence(
    sample_rate: int = 24000,
    silence_threshold: float = 0.012,
    silence_seconds: float = 1.2,
    max_record_seconds: float = 30.0,
    block_seconds: float = 0.1,
) -> RecordingResult:
    np, sd = _import_audio_stack()

    block_samples = max(1, int(sample_rate * block_seconds))
    silence_blocks_needed = max(1, int(silence_seconds / block_seconds))
    max_blocks = max(1, int(max_record_seconds / block_seconds))

    frames: list = []
    silent_blocks = 0
    heard_voice = False

    try:
        with sd.InputStream(samplerate=sample_rate, channels=1, dtype="float32") as stream:
            for _ in range(max_blocks):
                block, _overflowed = stream.read(block_samples)
                mono = block[:, 0] if block.ndim > 1 else block
                frames.append(mono.copy())

                rms = float(np.sqrt(np.mean(mono * mono))) if mono.size else 0.0
                if rms >= silence_threshold:
                    heard_voice = True
                    silent_blocks = 0
                elif heard_voice:
                    silent_blocks += 1
                    if silent_blocks >= silence_blocks_needed:
                        break
    except Exception as exc:  # pragma: no cover — depends on local hardware
        raise VoiceIOError(f"Microphone capture failed: {exc}") from exc

    if not frames:
        return RecordingResult(wav_bytes=b"", duration_seconds=0.0, sample_rate=sample_rate)

    audio = np.concatenate(frames)
    pcm16 = np.clip(audio * 32767.0, -32768, 32767).astype("<i2").tobytes()

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16)

    return RecordingResult(
        wav_bytes=buf.getvalue(),
        duration_seconds=len(audio) / float(sample_rate),
        sample_rate=sample_rate,
    )
