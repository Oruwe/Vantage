"""Session orchestrator: one or many turns against the manager agent.

Two surfaces:
  - ``ask_once`` — pure text in / text out. No audio deps required.
  - ``voice_turn`` — mic → STT → chat/stream → TTS → speaker.

Both persist the ``conversation_id`` back to the config so follow-up turns
continue the same chat.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from employer_voice import config as _cfg
from employer_voice.replica_client import ChatResult, ReplicaClient


@dataclass
class StreamSummary:
    conversation_id: str = ""
    assistant_text: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    stop_reason: str = ""
    loops: int = 0
    budget_exhausted: bool = False
    pending_approval: bool = False
    pending_tool_use_id: str = ""


def _client_from_config(cfg: _cfg.VoiceConfig) -> ReplicaClient:
    if not cfg.replica.manager_token:
        raise RuntimeError(
            "No manager_token configured. Run 'employer-voice init --manager-token vsm_...' first."
        )
    return ReplicaClient(cfg.replica.url, cfg.replica.manager_token)


async def ask_once(
    message: str,
    *,
    cfg: _cfg.VoiceConfig | None = None,
    config_path: Path | None = None,
) -> ChatResult:
    cfg = cfg or _cfg.load(config_path)
    client = _client_from_config(cfg)
    result = await client.chat(
        message=message,
        conversation_id=cfg.conversation_id or None,
    )
    if result.conversation_id:
        _cfg.update_conversation_id(result.conversation_id, config_path)
    return result


async def ask_stream(
    message: str,
    *,
    cfg: _cfg.VoiceConfig | None = None,
    config_path: Path | None = None,
    on_event: Callable[[dict], None] | None = None,
) -> StreamSummary:
    cfg = cfg or _cfg.load(config_path)
    client = _client_from_config(cfg)

    summary = StreamSummary()
    text_parts: list[str] = []

    async for ev in client.chat_stream(
        message=message,
        conversation_id=cfg.conversation_id or None,
    ):
        if on_event is not None:
            on_event(ev)
        name = ev.get("event")
        if name == "started":
            summary.conversation_id = ev.get("conversation_id", "")
        elif name == "message_delta":
            text_parts.append(ev.get("content", ""))
        elif name == "tool_call":
            summary.tool_calls.append({
                "id": ev.get("id"),
                "tool": ev.get("tool"),
                "args": ev.get("args", {}),
                "requires_confirmation": bool(ev.get("requires_confirmation")),
            })
        elif name == "done":
            summary.conversation_id = ev.get("conversation_id", summary.conversation_id)
            summary.stop_reason = ev.get("stop_reason", "")
            summary.loops = int(ev.get("loops", 0))
            summary.budget_exhausted = bool(ev.get("budget_exhausted"))
            summary.pending_approval = bool(ev.get("pending_approval"))
            summary.pending_tool_use_id = ev.get("pending_tool_use_id", "")
        elif name == "error":
            raise RuntimeError(ev.get("detail", "stream error"))

    summary.assistant_text = "".join(text_parts).strip()

    if summary.conversation_id:
        _cfg.update_conversation_id(summary.conversation_id, config_path)
    return summary


async def voice_turn(
    *,
    cfg: _cfg.VoiceConfig | None = None,
    config_path: Path | None = None,
    speak_response: bool = True,
) -> StreamSummary:
    """Capture one spoken utterance, send it to the agent, speak the reply.

    Imports audio modules lazily so the function raises a clear
    ``VoiceIOError`` only when actually invoked without the hardware stack.
    """
    cfg = cfg or _cfg.load(config_path)

    from employer_voice.voice import VoiceIOError  # re-export, keeps import local
    from employer_voice.voice import mic as _mic
    from employer_voice.voice import transcription as _stt

    api_key = cfg.openai_api_key
    if not api_key:
        raise VoiceIOError(
            f"Set your OpenAI key in env var {cfg.voice.openai_api_key_env}"
        )

    rec = _mic.record_until_silence(
        sample_rate=cfg.voice.sample_rate,
        silence_threshold=cfg.voice.silence_threshold,
        silence_seconds=cfg.voice.silence_seconds,
        max_record_seconds=cfg.voice.max_record_seconds,
    )
    if not rec.wav_bytes:
        raise VoiceIOError("No audio captured; is the microphone muted?")

    text = await _stt.transcribe_wav(rec.wav_bytes, api_key=api_key, model=cfg.voice.stt_model)
    if not text:
        raise VoiceIOError("Transcription returned empty text")

    summary = await ask_stream(text, cfg=cfg, config_path=config_path)
    summary.assistant_text = summary.assistant_text or ""

    if speak_response and summary.assistant_text:
        from employer_voice.voice import synthesis as _tts
        from employer_voice.voice import speaker as _spk
        mp3 = await _tts.synthesize(
            summary.assistant_text,
            api_key=api_key,
            model=cfg.voice.tts_model,
            voice=cfg.voice.tts_voice,
        )
        _spk.play_audio_bytes(mp3)

    return summary
