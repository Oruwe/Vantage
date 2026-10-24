from __future__ import annotations

import os
import sys
import tomllib
import uuid
from dataclasses import dataclass, field
from pathlib import Path


def _vantage_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
        return base / "Vantage"
    return Path.home() / ".vantage"


@dataclass
class LLMConfig:
    provider: str = "groq"
    model: str = "openai/gpt-oss-120b"
    api_key_env: str = "GROQ_API_KEY"
    endpoint: str = ""


@dataclass
class EmbeddingsConfig:
    provider: str = "ollama"
    model: str = "nomic-embed-text"
    endpoint: str = "http://localhost:11434"


@dataclass
class UIConfig:
    bind_address: str = "127.0.0.1"
    port: int = 7474


@dataclass
class AuditConfig:
    enabled: bool = True
    retention_days: int = 30


@dataclass
class Config:
    developer_id: str
    display_name: str = ""
    session_dirs: list[str] = field(default_factory=list)
    project_roots: list[str] = field(default_factory=list)
    log_level: str = "INFO"
    llm: LLMConfig = field(default_factory=LLMConfig)
    embeddings: EmbeddingsConfig = field(default_factory=EmbeddingsConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    audit: AuditConfig = field(default_factory=AuditConfig)

    @property
    def vantage_dir(self) -> Path:
        return _vantage_dir()

    @property
    def state_dir(self) -> Path:
        return self.vantage_dir / "state"

    @property
    def audit_dir(self) -> Path:
        return self.vantage_dir / "audit"

    @property
    def log_dir(self) -> Path:
        return self.vantage_dir / "logs"

    @property
    def sqlite_path(self) -> Path:
        return self.vantage_dir / "vantage.db"

    @property
    def machine_id(self) -> str:
        """Persistent UUID identifying this machine. Created on first access."""
        mid_file = self.state_dir / "machine_id"
        if mid_file.exists():
            return mid_file.read_text(encoding="utf-8").strip()
        mid = str(uuid.uuid4())
        mid_file.parent.mkdir(parents=True, exist_ok=True)
        mid_file.write_text(mid, encoding="utf-8")
        return mid

    @property
    def resolved_session_dirs(self) -> list[Path]:
        if self.session_dirs:
            return [Path(p).expanduser() for p in self.session_dirs]
        dirs = []
        claude_dir = Path.home() / ".claude" / "projects"
        if claude_dir.exists():
            dirs.append(claude_dir)
        codex_dir = Path.home() / ".codex" / "sessions"
        if codex_dir.exists():
            dirs.append(codex_dir)
        return dirs


def load(path: Path | None = None) -> Config:
    config_path = path or (_vantage_dir() / "config.toml")

    if not config_path.exists():
        return Config(developer_id=str(uuid.uuid4()))

    with open(config_path, "rb") as f:
        data = tomllib.load(f)

    llm_data = data.get("llm", {})
    emb_data = data.get("embeddings", {})
    ui_data = data.get("ui", {})
    audit_data = data.get("audit", {})

    return Config(
        developer_id=data.get("developer_id", str(uuid.uuid4())),
        display_name=data.get("display_name", ""),
        session_dirs=data.get("session_dirs", []),
        project_roots=data.get("project_roots", []),
        log_level=data.get("log_level", "INFO"),
        llm=LLMConfig(
            provider=llm_data.get("provider", "anthropic"),
            model=llm_data.get("model", "claude-haiku-4-5-20251001"),
            api_key_env=llm_data.get("api_key_env", "ANTHROPIC_API_KEY"),
            endpoint=llm_data.get("endpoint", ""),
        ),
        embeddings=EmbeddingsConfig(
            provider=emb_data.get("provider", "ollama"),
            model=emb_data.get("model", "nomic-embed-text"),
            endpoint=emb_data.get("endpoint", "http://localhost:11434"),
        ),
        ui=UIConfig(
            bind_address=ui_data.get("bind_address", "127.0.0.1"),
            port=ui_data.get("port", 7474),
        ),
        audit=AuditConfig(
            enabled=audit_data.get("enabled", True),
            retention_days=audit_data.get("retention_days", 30),
        ),
    )


def write_default(path: Path | None = None) -> Path:
    config_path = path or (_vantage_dir() / "config.toml")
    config_path.parent.mkdir(parents=True, exist_ok=True)

    developer_id = str(uuid.uuid4())
    config_path.write_text(
        f"""developer_id = "{developer_id}"
display_name = ""
session_dirs = []
project_roots = []
log_level = "INFO"

[llm]
provider = "groq"
model = "openai/gpt-oss-120b"
api_key_env = "GROQ_API_KEY"
endpoint = ""

[embeddings]
provider = "ollama"
model = "nomic-embed-text"
endpoint = "http://localhost:11434"

[ui]
bind_address = "127.0.0.1"
port = 7474

[audit]
enabled = true
retention_days = 30
""",
        encoding="utf-8",
    )
    return config_path
