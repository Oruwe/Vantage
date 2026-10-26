"""employer-voice — voice frontend for the Vantage manager agent.

Connects a local microphone + speaker to the shadow-replica manager agent
over its /ui/api/chat/stream SSE endpoint. Also exposes a text-only `ask`
mode so the package is useful (and testable) without audio hardware.
"""
from __future__ import annotations

from employer_voice.cli import app


def main() -> None:
    app()


__all__ = ["main", "app"]
