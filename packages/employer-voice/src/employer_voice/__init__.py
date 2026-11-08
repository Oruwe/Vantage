"""employer-voice — text chat frontend for the Vantage manager agent.

Lightweight CLI that connects to the shadow-replica over its
``/ui/api/chat`` and ``/ui/api/chat/stream`` endpoints. No audio.
"""
from __future__ import annotations

from employer_voice.cli import app


def main() -> None:
    app()


__all__ = ["main", "app"]
