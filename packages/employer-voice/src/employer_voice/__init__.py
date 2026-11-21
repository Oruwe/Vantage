"""employer-voice — text chat frontend for the Vantage manager agent.

Lightweight CLI that connects to the shadow-replica over its
``/ui/api/chat`` and ``/ui/api/chat/stream`` endpoints. No audio.
"""
from __future__ import annotations

from employer_voice.cli import app


def main() -> None:
    import sys
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    app()


__all__ = ["main", "app"]
