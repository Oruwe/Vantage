from __future__ import annotations

import tomllib
from pathlib import Path

from vantage.schemas import Channel, RedactionLevel, SharingPolicy


def _policies_dir(vantage_dir: Path) -> Path:
    return vantage_dir / "policies"


def load_policy(subscriber_id: str, vantage_dir: Path) -> SharingPolicy:
    """Load subscriber policy from TOML. Returns a permissive default if no file exists."""
    policy_path = _policies_dir(vantage_dir) / f"{subscriber_id}.toml"
    if not policy_path.exists():
        return SharingPolicy(
            subscriber_id=subscriber_id,
            channels=[Channel.WEEKLY_DIGEST],
            redaction_level=RedactionLevel.STANDARD,
            auto_approve=False,
        )
    with open(policy_path, "rb") as f:
        data = tomllib.load(f)
    return SharingPolicy(
        subscriber_id=subscriber_id,
        channels=[Channel(c) for c in data.get("channels", ["weekly_digest"])],
        redaction_level=RedactionLevel(data.get("redaction_level", "standard")),
        auto_approve=data.get("auto_approve", False),
    )


def save_policy(policy: SharingPolicy, vantage_dir: Path) -> Path:
    """Save subscriber policy to TOML."""
    policies_dir = _policies_dir(vantage_dir)
    policies_dir.mkdir(parents=True, exist_ok=True)
    policy_path = policies_dir / f"{policy.subscriber_id}.toml"
    channels_toml = "[" + ", ".join(f'"{c.value}"' for c in policy.channels) + "]"
    policy_path.write_text(
        f'subscriber_id = "{policy.subscriber_id}"\n'
        f"channels = {channels_toml}\n"
        f'redaction_level = "{policy.redaction_level.value}"\n'
        f"auto_approve = {str(policy.auto_approve).lower()}\n",
        encoding="utf-8",
    )
    return policy_path
