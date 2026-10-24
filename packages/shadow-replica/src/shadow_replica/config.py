from __future__ import annotations

import secrets
import sys
import tomllib
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from shadow_replica.notify.notifier import NotificationConfig


def _replica_config_dir() -> Path:
    if sys.platform == "win32":
        import os
        base = Path(os.environ.get("APPDATA", Path.home()))
        return base / "VantageReplica"
    return Path.home() / ".vantage-replica"


@dataclass
class QdrantConfig:
    url: str = "http://localhost:6333"
    enabled: bool = True


@dataclass
class EmbeddingsConfig:
    provider: str = "ollama"
    model: str = "nomic-embed-text"
    endpoint: str = "http://localhost:11434"


@dataclass
class LLMConfig:
    provider: str = ""
    model: str = ""
    api_key_env: str = ""
    endpoint: str = ""


@dataclass
class TrustedDev:
    developer_id: str
    name: str
    token_value: str  # vsub_... bearer token from the dev
    subscriber_id: str = ""  # sub_... ID on the dev's side (needed for probe policy lookup)


@dataclass
class ReplicaConfig:
    replica_id: str
    bind_host: str = "0.0.0.0"
    bind_port: int = 7475
    manager_token: str = ""
    trusted_devs: list[TrustedDev] = field(default_factory=list)
    notifications: list[NotificationConfig] = field(default_factory=list)
    qdrant: QdrantConfig = field(default_factory=QdrantConfig)
    embeddings: EmbeddingsConfig = field(default_factory=EmbeddingsConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)

    @property
    def config_dir(self) -> Path:
        return _replica_config_dir()

    @property
    def sqlite_path(self) -> Path:
        return self.config_dir / "replica.db"


def load(path: Path | None = None) -> ReplicaConfig:
    config_path = path or (_replica_config_dir() / "replica.toml")
    if not config_path.exists():
        return ReplicaConfig(
            replica_id=str(uuid.uuid4()),
            manager_token="vsm_" + secrets.token_urlsafe(24),
        )

    with open(config_path, "rb") as f:
        data = tomllib.load(f)

    trusted_devs = [
        TrustedDev(
            developer_id=d["developer_id"],
            name=d.get("name", d["developer_id"]),
            token_value=d["token_value"],
            subscriber_id=d.get("subscriber_id", ""),
        )
        for d in data.get("trusted_devs", [])
    ]

    notifications = [
        NotificationConfig(
            type=n.get("type", ""),
            url=n.get("url", ""),
            host=n.get("host", ""),
            port=int(n.get("port", 587)),
            username=n.get("username", ""),
            password_env=n.get("password_env", ""),
            from_addr=n.get("from_addr", ""),
            to_addrs=list(n.get("to_addrs", [])),
            artifact_types=list(n.get("artifact_types", [])),
        )
        for n in data.get("notifications", [])
    ]

    manager_token = data.get("manager_token", "")
    generated = False
    if not manager_token:
        manager_token = "vsm_" + secrets.token_urlsafe(24)
        generated = True

    qdrant_data = data.get("qdrant", {})
    emb_data = data.get("embeddings", {})
    llm_data = data.get("llm", {})

    cfg = ReplicaConfig(
        replica_id=data.get("replica_id", str(uuid.uuid4())),
        bind_host=data.get("bind_host", "0.0.0.0"),
        bind_port=data.get("bind_port", 7475),
        manager_token=manager_token,
        trusted_devs=trusted_devs,
        notifications=notifications,
        qdrant=QdrantConfig(
            url=qdrant_data.get("url", "http://localhost:6333"),
            enabled=qdrant_data.get("enabled", True),
        ),
        embeddings=EmbeddingsConfig(
            provider=emb_data.get("provider", "ollama"),
            model=emb_data.get("model", "nomic-embed-text"),
            endpoint=emb_data.get("endpoint", "http://localhost:11434"),
        ),
        llm=LLMConfig(
            provider=llm_data.get("provider", ""),
            model=llm_data.get("model", ""),
            api_key_env=llm_data.get("api_key_env", ""),
            endpoint=llm_data.get("endpoint", ""),
        ),
    )
    if generated:
        _write(cfg, config_path)
    return cfg


def add_trusted_dev(
    developer_id: str,
    name: str,
    token_value: str,
    subscriber_id: str = "",
    path: Path | None = None,
) -> Path:
    """Add a trusted dev to the replica config TOML and return the config path."""
    config_path = path or (_replica_config_dir() / "replica.toml")
    config_path.parent.mkdir(parents=True, exist_ok=True)

    cfg = load(config_path)
    updated = False
    for dev in cfg.trusted_devs:
        if dev.developer_id == developer_id:
            dev.token_value = token_value
            dev.name = name
            if subscriber_id:
                dev.subscriber_id = subscriber_id
            updated = True
            break
    if not updated:
        cfg.trusted_devs.append(TrustedDev(
            developer_id=developer_id,
            name=name,
            token_value=token_value,
            subscriber_id=subscriber_id,
        ))

    _write(cfg, config_path)
    return config_path


def _write(cfg: ReplicaConfig, path: Path) -> None:
    lines = [
        f'replica_id = "{cfg.replica_id}"\n',
        f'bind_host = "{cfg.bind_host}"\n',
        f"bind_port = {cfg.bind_port}\n",
        f'manager_token = "{cfg.manager_token}"\n',
        "\n",
        "[qdrant]\n",
        f'url = "{cfg.qdrant.url}"\n',
        f"enabled = {'true' if cfg.qdrant.enabled else 'false'}\n",
        "\n",
        "[embeddings]\n",
        f'provider = "{cfg.embeddings.provider}"\n',
        f'model = "{cfg.embeddings.model}"\n',
        f'endpoint = "{cfg.embeddings.endpoint}"\n',
        "\n",
    ]
    if cfg.llm.provider:
        lines += [
            "[llm]\n",
            f'provider = "{cfg.llm.provider}"\n',
            f'model = "{cfg.llm.model}"\n',
            f'api_key_env = "{cfg.llm.api_key_env}"\n',
            f'endpoint = "{cfg.llm.endpoint}"\n',
            "\n",
        ]
    for dev in cfg.trusted_devs:
        lines += [
            "[[trusted_devs]]\n",
            f'developer_id = "{dev.developer_id}"\n',
            f'name = "{dev.name}"\n',
            f'token_value = "{dev.token_value}"\n',
        ]
        if dev.subscriber_id:
            lines.append(f'subscriber_id = "{dev.subscriber_id}"\n')
        lines.append("\n")
    for n in cfg.notifications:
        lines += ["[[notifications]]\n", f'type = "{n.type}"\n']
        if n.url:
            lines.append(f'url = "{n.url}"\n')
        if n.host:
            lines.append(f'host = "{n.host}"\n')
        if n.port != 587:
            lines.append(f"port = {n.port}\n")
        if n.username:
            lines.append(f'username = "{n.username}"\n')
        if n.password_env:
            lines.append(f'password_env = "{n.password_env}"\n')
        if n.from_addr:
            lines.append(f'from_addr = "{n.from_addr}"\n')
        if n.to_addrs:
            addrs = ", ".join(f'"{a}"' for a in n.to_addrs)
            lines.append(f"to_addrs = [{addrs}]\n")
        if n.artifact_types:
            types = ", ".join(f'"{t}"' for t in n.artifact_types)
            lines.append(f"artifact_types = [{types}]\n")
        lines.append("\n")
    path.write_text("".join(lines), encoding="utf-8")
