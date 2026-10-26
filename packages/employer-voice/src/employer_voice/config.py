"""Config for employer-voice stored as TOML.

Location:
  - Windows: %APPDATA%\\VantageVoice\\voice.toml
  - Unix:    ~/.vantage-voice/voice.toml

The config holds how to reach the shadow-replica (URL + manager token) and
how to talk to OpenAI (env var holding the key + which models to use for
Whisper STT and TTS). An empty default config is generated on first load so
``employer-voice config`` has something to print.
"""
from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path


def _config_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
        return base / "VantageVoice"
    return Path.home() / ".vantage-voice"


def _default_config_path() -> Path:
    return _config_dir() / "voice.toml"


@dataclass
class ReplicaTarget:
    url: str = "http://127.0.0.1:7475"
    manager_token: str = ""


@dataclass
class VoiceSettings:
    openai_api_key_env: str = "OPENAI_API_KEY"
    stt_model: str = "whisper-1"
    tts_model: str = "tts-1"
    tts_voice: str = "alloy"
    sample_rate: int = 24000
    silence_threshold: float = 0.012
    silence_seconds: float = 1.2
    max_record_seconds: float = 30.0


@dataclass
class VoiceConfig:
    replica: ReplicaTarget = field(default_factory=ReplicaTarget)
    voice: VoiceSettings = field(default_factory=VoiceSettings)
    conversation_id: str = ""

    @property
    def openai_api_key(self) -> str:
        return os.environ.get(self.voice.openai_api_key_env, "").strip()

    def resolved_bearer(self) -> str:
        return self.replica.manager_token.strip()


def load(path: Path | None = None) -> VoiceConfig:
    config_path = path or _default_config_path()
    if not config_path.exists():
        return VoiceConfig()

    with open(config_path, "rb") as f:
        data = tomllib.load(f)

    rep = data.get("replica", {})
    voi = data.get("voice", {})
    return VoiceConfig(
        replica=ReplicaTarget(
            url=rep.get("url", "http://127.0.0.1:7475"),
            manager_token=rep.get("manager_token", ""),
        ),
        voice=VoiceSettings(
            openai_api_key_env=voi.get("openai_api_key_env", "OPENAI_API_KEY"),
            stt_model=voi.get("stt_model", "whisper-1"),
            tts_model=voi.get("tts_model", "tts-1"),
            tts_voice=voi.get("tts_voice", "alloy"),
            sample_rate=int(voi.get("sample_rate", 24000)),
            silence_threshold=float(voi.get("silence_threshold", 0.012)),
            silence_seconds=float(voi.get("silence_seconds", 1.2)),
            max_record_seconds=float(voi.get("max_record_seconds", 30.0)),
        ),
        conversation_id=data.get("conversation_id", ""),
    )


def save(cfg: VoiceConfig, path: Path | None = None) -> Path:
    config_path = path or _default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    if cfg.conversation_id:
        lines.append(f'conversation_id = "{cfg.conversation_id}"\n\n')
    lines += [
        "[replica]\n",
        f'url = "{cfg.replica.url}"\n',
        f'manager_token = "{cfg.replica.manager_token}"\n',
        "\n",
        "[voice]\n",
        f'openai_api_key_env = "{cfg.voice.openai_api_key_env}"\n',
        f'stt_model = "{cfg.voice.stt_model}"\n',
        f'tts_model = "{cfg.voice.tts_model}"\n',
        f'tts_voice = "{cfg.voice.tts_voice}"\n',
        f"sample_rate = {cfg.voice.sample_rate}\n",
        f"silence_threshold = {cfg.voice.silence_threshold}\n",
        f"silence_seconds = {cfg.voice.silence_seconds}\n",
        f"max_record_seconds = {cfg.voice.max_record_seconds}\n",
    ]

    config_path.write_text("".join(lines), encoding="utf-8")
    try:
        if sys.platform != "win32":
            os.chmod(config_path, 0o600)
    except OSError:
        pass
    return config_path


def as_dict(cfg: VoiceConfig) -> dict:
    return {
        "replica": asdict(cfg.replica),
        "voice": asdict(cfg.voice),
        "conversation_id": cfg.conversation_id,
    }


def update_conversation_id(conversation_id: str, path: Path | None = None) -> Path:
    """Persist the last conversation_id so follow-up turns can resume it."""
    cfg = load(path)
    if cfg.conversation_id == conversation_id:
        return path or _default_config_path()
    cfg.conversation_id = conversation_id
    return save(cfg, path)
