"""Voice I/O for employer-voice.

All submodules soft-import their heavy deps (sounddevice, numpy) and raise
a clear ``VoiceIOError`` with install hints when they are missing — so the
rest of the package (config, replica client, text-mode CLI) remains
importable in CI and on headless machines.
"""
from __future__ import annotations


class VoiceIOError(RuntimeError):
    """Raised when audio I/O is requested but local hardware/deps are unavailable."""


__all__ = ["VoiceIOError"]
