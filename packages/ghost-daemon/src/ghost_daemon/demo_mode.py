"""Offline demo mode — seeds configs, pairs ghost ↔ replica, runs both in-process.

No internet, no Qdrant, no Ollama, no API keys. Uses the ``mock`` LLM
provider on both sides and the existing in-memory fallbacks for Qdrant.

Call :func:`run_demo` from the CLI; it blocks until Ctrl+C.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path


# Planted secret the scrubber will catch. Not a real credential — the AKIA
# prefix matches the regex but the suffix is clearly dummy.
_DEMO_AWS_KEY = "AKIAEXAMPLEDEMO9999"


def _seed_session(sessions_dir: Path) -> Path:
    """Write a plausible Claude session JSONL that embeds the planted key."""
    sessions_dir.mkdir(parents=True, exist_ok=True)
    f = sessions_dir / "demo-session.jsonl"
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    turns = [
        {
            "role": "user",
            "content": "Let's add the S3 upload path. Here's my sandbox credential I was going to use for the test run.",
            "timestamp": now,
        },
        {
            "role": "assistant",
            "content": (
                "Noted. I'll wire up the client against the sandbox. For reference the "
                f"key you pasted was {_DEMO_AWS_KEY} — I'll keep it out of the commit."
            ),
            "timestamp": now,
        },
        {
            "role": "user",
            "content": "Right. Factor the uploader out of handlers/s3.py and add a unit test.",
            "timestamp": now,
        },
        {
            "role": "assistant",
            "content": (
                "Done — moved the upload logic into services/uploader.py and added "
                "tests/test_uploader.py covering the happy path and a timeout case."
            ),
            "timestamp": now,
        },
    ]
    with open(f, "w", encoding="utf-8") as fh:
        for t in turns:
            fh.write(json.dumps(t) + "\n")
    return f


def _write_ghost_config(
    ghost_home: Path,
    ui_port: int,
    sessions_root: Path,
) -> None:
    """Write a minimal config.toml pointing the ghost at mock LLM + the seeded sessions."""
    ghost_home.mkdir(parents=True, exist_ok=True)
    (ghost_home / "config.toml").write_text(
        f"""\
developer_id = "demo-alice"
display_name = "Demo Alice"
session_dirs = [{json.dumps(str(sessions_root))}]
log_level = "WARNING"

[llm]
provider = "mock"
model = "mock"
api_key_env = ""

[embeddings]
provider = "mock"
model = "mock"
endpoint = ""

[ui]
bind_address = "127.0.0.1"
port = {ui_port}

[qdrant]
enabled = false
url = "http://127.0.0.1:1"
""",
        encoding="utf-8",
    )


def _write_replica_config(
    replica_home: Path,
    bind_port: int,
    manager_token: str,
) -> Path:
    """Write a minimal replica.toml. Returns the path."""
    replica_home.mkdir(parents=True, exist_ok=True)
    p = replica_home / "replica.toml"
    p.write_text(
        f"""\
replica_id = "demo-replica"
bind_host = "127.0.0.1"
bind_port = {bind_port}
manager_token = "{manager_token}"

[qdrant]
enabled = false
url = "http://127.0.0.1:1"

[embeddings]
provider = "mock"
model = "mock"
endpoint = ""

[llm]
provider = "mock"
model = "mock"
api_key_env = ""
""",
        encoding="utf-8",
    )
    return p


async def _serve_uvicorn(app, host: str, port: int) -> None:
    """Run a uvicorn.Server for `app` on this asyncio loop."""
    import uvicorn
    cfg = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(cfg)
    await server.serve()


async def _run_both(ghost_app, replica_app, ghost_port: int, replica_port: int) -> None:
    await asyncio.gather(
        _serve_uvicorn(replica_app, "127.0.0.1", replica_port),
        _serve_uvicorn(ghost_app, "127.0.0.1", ghost_port),
    )


def run_demo(
    home: str = "",
    ghost_port: int = 7474,
    replica_port: int = 7475,
    clean: bool = True,
) -> None:
    """Seed configs + sessions, pair the ghost ↔ replica, then run both."""
    base = Path(home).expanduser() if home else Path(tempfile.gettempdir()) / "vantage-demo"
    if clean and base.exists():
        shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    ghost_home = base / "ghost"
    replica_home = base / "replica"
    sessions_root = base / "sessions"
    sessions_dir = sessions_root / "demo-project"

    # Steer both processes at the demo dirs.
    os.environ["VANTAGE_HOME"] = str(ghost_home)
    os.environ["VANTAGE_REPLICA_HOME"] = str(replica_home)

    _seed_session(sessions_dir)
    _write_ghost_config(ghost_home, ghost_port, sessions_root)
    manager_token = "vsm_demo_" + secrets.token_hex(6)
    replica_toml = _write_replica_config(replica_home, replica_port, manager_token)

    # Import after env vars are set so module-level config lookups see the demo dirs.
    from ghost_daemon import config as ghost_cfg_mod
    from ghost_daemon.daemon import GhostDaemon
    from ghost_daemon.subscribers.registry import SubscriberRegistry
    from shadow_replica import config as replica_cfg_mod
    from shadow_replica.app import build_app as build_replica_app
    from vantage.schemas import Channel

    gc = ghost_cfg_mod.load()
    # Ensure the SQLite dir exists before the registry writes to it.
    gc.sqlite_path.parent.mkdir(parents=True, exist_ok=True)

    receive_url = f"http://127.0.0.1:{replica_port}"
    reg = SubscriberRegistry(gc.sqlite_path)
    identity = reg.add(
        name="Demo Manager",
        receive_url=receive_url,
        channels=[
            Channel.WEEKLY_DIGEST,
            Channel.COMMIT_REPORT,
            Channel.STANDUP,
            Channel.PR_DRAFT,
            Channel.QUICK_NOTE,
        ],
    )

    # Teach the replica to trust this ghost. We set subscriber_id to the one
    # we just issued so the live-probe policy can look the dev up.
    replica_cfg_mod.add_trusted_dev(
        developer_id=gc.developer_id,
        name=gc.display_name or gc.developer_id,
        token_value=identity.token_value,
        subscriber_id=identity.subscriber_id,
        path=replica_toml,
    )

    # Build the two apps. Replica goes first so the ghost can push to a
    # live endpoint as soon as the user approves a draft.
    rcfg = replica_cfg_mod.load(replica_toml)
    replica_app = build_replica_app(rcfg)

    daemon = GhostDaemon(gc)
    ghost_app = daemon.get_app()

    # Print with an encoding-safe writer so Windows cp1252 consoles don't
    # crash on non-ASCII. Falls back to a plain write with explicit replace.
    def _say(s: str) -> None:
        try:
            print(s)
        except UnicodeEncodeError:
            import sys as _sys
            _sys.stdout.buffer.write(s.encode("utf-8", errors="replace") + b"\n")

    _say("")
    _say("Vantage demo mode is running - no network, no API keys.")
    _say("")
    _say(f"  Developer UI (ghost):   http://127.0.0.1:{ghost_port}")
    _say(f"  Manager UI (replica):   http://127.0.0.1:{replica_port}")
    _say(f"  Manager token:          {manager_token}")
    _say("")
    _say("Seeded with a session that includes a planted AWS key (AKIA...).")
    _say(f"Demo state lives under:   {base}")
    _say("")
    _say("Try it:")
    _say("  1. Open the developer UI and generate a draft (vantage-ghost digest).")
    _say("  2. Open a draft -> X-Ray view to see the scrubber replace the key.")
    _say("  3. Approve it; the manager UI timeline will show it immediately.")
    _say("")
    _say("Press Ctrl+C to stop.")
    _say("")

    try:
        asyncio.run(_run_both(ghost_app, replica_app, ghost_port, replica_port))
    except KeyboardInterrupt:
        print("\nDemo stopped.")
