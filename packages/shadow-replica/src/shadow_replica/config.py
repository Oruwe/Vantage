from __future__ import annotations

import sys
import tomllib
import uuid
from dataclasses import dataclass, field
from pathlib import Path


def _replica_config_dir() -> Path:
    if sys.platform == "win32":
        import os
        base = Path(os.environ.get("APPDATA", Path.home()))
        return base / "VantageReplica"
    return Path.home() / ".vantage-replica"


@dataclass
class TrustedDev:
    developer_id: str
    name: str
    token_value: str  # vsub_... bearer token from the dev


@dataclass
class ReplicaConfig:
    replica_id: str
    bind_host: str = "0.0.0.0"
    bind_port: int = 7475
    trusted_devs: list[TrustedDev] = field(default_factory=list)

    @property
    def config_dir(self) -> Path:
        return _replica_config_dir()

    @property
    def sqlite_path(self) -> Path:
        return self.config_dir / "replica.db"


def load(path: Path | None = None) -> ReplicaConfig:
    config_path = path or (_replica_config_dir() / "replica.toml")
    if not config_path.exists():
        return ReplicaConfig(replica_id=str(uuid.uuid4()))

    with open(config_path, "rb") as f:
        data = tomllib.load(f)

    trusted_devs = [
        TrustedDev(
            developer_id=d["developer_id"],
            name=d.get("name", d["developer_id"]),
            token_value=d["token_value"],
        )
        for d in data.get("trusted_devs", [])
    ]

    return ReplicaConfig(
        replica_id=data.get("replica_id", str(uuid.uuid4())),
        bind_host=data.get("bind_host", "0.0.0.0"),
        bind_port=data.get("bind_port", 7475),
        trusted_devs=trusted_devs,
    )


def add_trusted_dev(developer_id: str, name: str, token_value: str, path: Path | None = None) -> Path:
    """Add a trusted dev to the replica config TOML and return the config path."""
    config_path = path or (_replica_config_dir() / "replica.toml")
    config_path.parent.mkdir(parents=True, exist_ok=True)

    cfg = load(config_path)
    updated = False
    for dev in cfg.trusted_devs:
        if dev.developer_id == developer_id:
            dev.token_value = token_value
            dev.name = name
            updated = True
            break
    if not updated:
        cfg.trusted_devs.append(TrustedDev(developer_id=developer_id, name=name, token_value=token_value))

    _write(cfg, config_path)
    return config_path


def _write(cfg: ReplicaConfig, path: Path) -> None:
    lines = [
        f'replica_id = "{cfg.replica_id}"\n',
        f'bind_host = "{cfg.bind_host}"\n',
        f"bind_port = {cfg.bind_port}\n",
        "\n",
    ]
    for dev in cfg.trusted_devs:
        lines += [
            "[[trusted_devs]]\n",
            f'developer_id = "{dev.developer_id}"\n',
            f'name = "{dev.name}"\n',
            f'token_value = "{dev.token_value}"\n',
            "\n",
        ]
    path.write_text("".join(lines), encoding="utf-8")
