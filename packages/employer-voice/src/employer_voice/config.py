"""Config for employer-voice stored as TOML.

Location:
  - Windows: %APPDATA%\\VantageVoice\\voice.toml
  - Unix:    ~/.vantage-voice/voice.toml

Holds how to reach the shadow-replica (URL + manager token) and the last
``conversation_id`` so follow-up turns can resume the same chat. Pure text
chat frontend — no audio.
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
class VoiceConfig:
    replica: ReplicaTarget = field(default_factory=ReplicaTarget)
    conversation_id: str = ""

    def resolved_bearer(self) -> str:
        return self.replica.manager_token.strip()


def load(path: Path | None = None) -> VoiceConfig:
    config_path = path or _default_config_path()
    if not config_path.exists():
        return VoiceConfig()

    with open(config_path, "rb") as f:
        data = tomllib.load(f)

    rep = data.get("replica", {})
    return VoiceConfig(
        replica=ReplicaTarget(
            url=rep.get("url", "http://127.0.0.1:7475"),
            manager_token=rep.get("manager_token", ""),
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
        "conversation_id": cfg.conversation_id,
    }


def update_conversation_id(conversation_id: str, path: Path | None = None) -> Path:
    """Persist the last conversation_id so follow-up turns can resume it."""
    cfg = load(path)
    if cfg.conversation_id == conversation_id:
        return path or _default_config_path()
    cfg.conversation_id = conversation_id
    return save(cfg, path)
