from __future__ import annotations

import asyncio
import sys
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(name="vantage-ghost", help="Vantage — developer-first engineering journal.")

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _load() -> "tuple[object, object]":
    from ghost_daemon import config as _cfg_mod
    from ghost_daemon.daemon import GhostDaemon
    cfg = _cfg_mod.load()
    daemon = GhostDaemon(cfg)
    return cfg, daemon


def _parse_since(since: str) -> datetime:
    since = since.strip().lower()
    now = datetime.utcnow()
    mapping = {
        "today": now.replace(hour=0, minute=0, second=0, microsecond=0),
        "yesterday": (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0),
        "monday": now - timedelta(days=now.weekday()),
        "week": now - timedelta(weeks=1),
        "month": now - timedelta(days=30),
    }
    if since in mapping:
        return mapping[since]
    try:
        return datetime.fromisoformat(since)
    except ValueError:
        typer.echo(f"Cannot parse --since value: {since!r}. Try: today, monday, week, or an ISO date.", err=True)
        raise typer.Exit(1)


# ──────────────────────────────────────────────────────────────────────────────
# Commands
# ──────────────────────────────────────────────────────────────────────────────

@app.command()
def run():
    """Start the background daemon and local UI server."""
    from ghost_daemon import config as _cfg_mod
    from ghost_daemon.logging_setup import setup_logging
    from ghost_daemon.daemon import GhostDaemon

    cfg = _cfg_mod.load()
    setup_logging(cfg.log_dir, cfg.log_level, daemon_mode=False)
    daemon = GhostDaemon(cfg)

    typer.echo(f"Starting Vantage daemon  →  http://{cfg.ui.bind_address}:{cfg.ui.port}")
    asyncio.run(daemon.run_forever())


@app.command()
def digest(
    since: Annotated[str, typer.Option(help="Period start: today, monday, week, month, or ISO date")] = "monday",
    until: Annotated[str, typer.Option(help="Period end (default: now)")] = "now",
    repo: Annotated[str, typer.Option(help="Restrict to a specific repo path")] = "",
    open_ui: Annotated[bool, typer.Option("--open/--no-open", help="Open browser after generating")] = True,
):
    """Generate a weekly digest draft from your coding sessions."""
    from ghost_daemon import config as _cfg_mod
    from ghost_daemon.logging_setup import setup_logging
    from ghost_daemon.daemon import GhostDaemon
    from ghost_daemon.agents.digest_agent import DigestAgent

    cfg = _cfg_mod.load()
    setup_logging(cfg.log_dir, cfg.log_level)
    daemon = GhostDaemon(cfg)

    period_start = _parse_since(since)
    period_end = datetime.utcnow() if until == "now" else datetime.fromisoformat(until)

    typer.echo(f"Generating digest for {period_start.date()} → {period_end.date()} …")

    agent = DigestAgent(cfg, daemon.llm, daemon.project_map, daemon.tailer)

    async def _run():
        draft = await agent.run(period_start, period_end, repo_filter=repo or None)
        draft_id = daemon.gate.submit(draft)
        return draft_id

    draft_id = asyncio.run(_run())
    url = f"http://{cfg.ui.bind_address}:{cfg.ui.port}/draft/{draft_id}"
    typer.echo(f"Draft ready: {url}")

    if open_ui:
        import uvicorn as _uvicorn
        uv_cfg = _uvicorn.Config(
            app=daemon.get_app(),
            host=cfg.ui.bind_address,
            port=cfg.ui.port,
            log_level="warning",
        )
        server = _uvicorn.Server(uv_cfg)

        async def _serve():
            webbrowser.open(url)
            await server.serve()

        typer.echo("Review UI running — press Ctrl+C to stop.")
        asyncio.run(_serve())


@app.command()
def review(
    open_ui: Annotated[bool, typer.Option("--open/--no-open")] = True,
):
    """Open the local review UI to approve or reject pending drafts."""
    from ghost_daemon import config as _cfg_mod
    from ghost_daemon.daemon import GhostDaemon

    cfg = _cfg_mod.load()
    url = f"http://{cfg.ui.bind_address}:{cfg.ui.port}/"
    typer.echo(f"Review UI: {url}")
    if open_ui:
        import uvicorn as _uvicorn
        daemon = GhostDaemon(cfg)
        uv_cfg = _uvicorn.Config(app=daemon.get_app(), host=cfg.ui.bind_address, port=cfg.ui.port, log_level="warning")
        server = _uvicorn.Server(uv_cfg)

        async def _serve():
            webbrowser.open(url)
            await server.serve()

        typer.echo("Review UI running — press Ctrl+C to stop.")
        asyncio.run(_serve())


@app.command()
def audit(
    days: Annotated[int, typer.Option(help="Number of days to show")] = 7,
):
    """Open the local audit dashboard to inspect what was redacted and sent."""
    from ghost_daemon import config as _cfg_mod
    from ghost_daemon.daemon import GhostDaemon

    cfg = _cfg_mod.load()
    url = f"http://{cfg.ui.bind_address}:{cfg.ui.port}/audit?days={days}"
    typer.echo(f"Audit dashboard: {url}")
    import uvicorn as _uvicorn
    daemon = GhostDaemon(cfg)
    uv_cfg = _uvicorn.Config(app=daemon.get_app(), host=cfg.ui.bind_address, port=cfg.ui.port, log_level="warning")
    server = _uvicorn.Server(uv_cfg)

    async def _serve():
        webbrowser.open(url)
        await server.serve()

    typer.echo("Audit UI running — press Ctrl+C to stop.")
    asyncio.run(_serve())


@app.command("key-check")
def key_check(
    env_var: Annotated[str, typer.Argument()] = "XAI_API_KEY",
):
    """Show where a key is being read from and its first 8 characters."""
    import os
    env_val = os.environ.get(env_var)
    if env_val:
        typer.echo(f"Source : environment variable")
        typer.echo(f"Prefix : {env_val[:8]}...")
        typer.echo(f"Length : {len(env_val)}")
        return
    try:
        import keyring
        kc_val = keyring.get_password("vantage", env_var)
    except Exception:
        kc_val = None
    if kc_val:
        typer.echo(f"Source : OS keychain")
        typer.echo(f"Prefix : {kc_val[:8]}...")
        typer.echo(f"Length : {len(kc_val)}")
    else:
        typer.echo(f"NOT FOUND — {env_var!r} is not set in env or keychain.", err=True)


@app.command("key")
def key_set(
    env_var: Annotated[str, typer.Argument(help="Env var name, e.g. XAI_API_KEY")] = "XAI_API_KEY",
    value: Annotated[str, typer.Option("--value", "-v", help="Key value (omit to be prompted)")] = "",
    delete: Annotated[bool, typer.Option("--delete", help="Remove the stored key")] = False,
):
    """Store (or remove) an API key in the OS keychain.

    Pass the key directly:   vantage-ghost key XAI_API_KEY --value xai-...
    Or be prompted for it:   vantage-ghost key XAI_API_KEY
    """
    from ghost_daemon.models.providers._keychain import store_api_key, delete_api_key

    if delete:
        delete_api_key(env_var)
        typer.echo(f"Removed {env_var!r} from keychain.")
        return

    if not value:
        value = typer.prompt(f"Enter value for {env_var}", hide_input=False)

    store_api_key(env_var, value.strip())
    typer.echo(f"Stored {env_var!r} in OS keychain (service='vantage').")
    typer.echo("You no longer need to set an environment variable.")


@app.command("check")
def check():
    """Verify the configured LLM provider responds correctly (quick API key test)."""
    from ghost_daemon import config as _cfg_mod
    from ghost_daemon.models.providers._keychain import resolve_api_key
    from ghost_daemon.models.registry import build_from_config

    cfg = _cfg_mod.load()
    typer.echo(f"Provider : {cfg.llm.provider}")
    typer.echo(f"Model    : {cfg.llm.model}")
    typer.echo(f"Key env  : {cfg.llm.api_key_env}")

    try:
        llm = build_from_config(cfg)
    except EnvironmentError as exc:
        typer.echo(f"\nKey not found:\n{exc}", err=True)
        raise typer.Exit(1)

    # For OpenAI-compat providers, list available models first
    if cfg.llm.provider in ("groq", "grok", "openrouter", "openai_compat"):
        from openai import AsyncOpenAI
        from ghost_daemon.models.providers._keychain import resolve_api_key
        from ghost_daemon.models.providers.openai_compat import _ENDPOINTS

        endpoint = cfg.llm.endpoint or _ENDPOINTS.get(cfg.llm.provider, ("", ""))[0]

        async def _list_models():
            api_key = resolve_api_key(cfg.llm.api_key_env)
            client = AsyncOpenAI(api_key=api_key, base_url=endpoint)
            models = await client.models.list()
            return sorted(m.id for m in models.data)

        try:
            model_ids = asyncio.run(_list_models())
            typer.echo(f"\nAvailable models ({len(model_ids)}):")
            for mid in model_ids:
                marker = " ◄ configured" if mid == cfg.llm.model else ""
                typer.echo(f"  {mid}{marker}")
            if cfg.llm.model not in model_ids:
                typer.echo(f"\n  ✗ '{cfg.llm.model}' not in list — update config.toml", err=True)
                raise typer.Exit(1)
        except typer.Exit:
            raise
        except Exception as exc:
            typer.echo(f"\nCould not list models: {exc}", err=True)

    async def _ping():
        return await llm.complete(
            messages=[{"role": "user", "content": "Reply with exactly: OK"}],
            max_tokens=10,
        )

    try:
        reply = asyncio.run(_ping())
        typer.echo(f"\nAPI response : {reply.strip()!r}")
        typer.echo("LLM connection OK.")
    except Exception as exc:
        typer.echo(f"\nLLM call failed: {exc}", err=True)
        raise typer.Exit(1)


subscribers_app = typer.Typer(help="Manage subscribers — who receives your approved artifacts.")
app.add_typer(subscribers_app, name="subscribers")


@subscribers_app.command("add")
def subscribers_add(
    name: Annotated[str, typer.Argument(help="Display name for this subscriber, e.g. 'alice'")],
    receive_url: Annotated[str, typer.Option("--receive-url", "-u", help="Base URL of their shadow-replica")] = "",
    channels: Annotated[list[str], typer.Option("--channels", "-c", help="Channels to enable (default: weekly_digest)")] = ["weekly_digest"],
):
    """Add a subscriber. Prints the token they need to configure their shadow-replica."""
    from ghost_daemon import config as _cfg_mod
    from ghost_daemon.subscribers.registry import SubscriberRegistry
    from vantage.schemas import Channel

    if not receive_url:
        receive_url = typer.prompt("Subscriber's shadow-replica URL (e.g. http://server:7475)")

    cfg = _cfg_mod.load()
    registry = SubscriberRegistry(cfg.sqlite_path)

    parsed_channels = []
    for ch in channels:
        try:
            parsed_channels.append(Channel(ch))
        except ValueError:
            typer.echo(f"Unknown channel {ch!r}. Valid: weekly_digest, commit_report, standup, pr_draft", err=True)
            raise typer.Exit(1)

    identity = registry.add(name, receive_url, parsed_channels)

    typer.echo(f"\nSubscriber '{identity.name}' added.")
    typer.echo(f"  ID         : {identity.subscriber_id}")
    typer.echo(f"  Channels   : {', '.join(c.value for c in identity.channels)}")
    typer.echo(f"  Receive URL: {identity.receive_url}")
    typer.echo(f"\nToken (give this to {name} — store it securely):")
    typer.echo(f"  {identity.token_value}")
    typer.echo(f"\nThey should run:")
    typer.echo(f"  shadow-replica import-dev {cfg.developer_id} --name \"{cfg.display_name or cfg.developer_id}\" --token {identity.token_value}")
    typer.echo(f"  shadow-replica serve")


@subscribers_app.command("list")
def subscribers_list():
    """List all subscribers."""
    from ghost_daemon import config as _cfg_mod
    from ghost_daemon.subscribers.registry import SubscriberRegistry
    import json

    cfg = _cfg_mod.load()
    registry = SubscriberRegistry(cfg.sqlite_path)
    rows = registry.list_all()

    if not rows:
        typer.echo("No subscribers configured. Use 'vantage-ghost subscribers add' to add one.")
        return

    typer.echo(f"{'NAME':<20} {'ID':<18} {'STATUS':<10} {'CHANNELS'}")
    typer.echo("-" * 70)
    for row in rows:
        channels = ", ".join(json.loads(row.channels_json))
        status = "revoked" if row.revoked_at else "active"
        typer.echo(f"{row.name:<20} {row.id:<18} {status:<10} {channels}")


@subscribers_app.command("show-token")
def subscribers_show_token(
    subscriber: Annotated[str, typer.Argument(help="Subscriber ID or name")],
):
    """Print the bearer token for a subscriber (needed for shadow-replica import-dev)."""
    from ghost_daemon import config as _cfg_mod
    from ghost_daemon.subscribers.registry import SubscriberRegistry

    cfg = _cfg_mod.load()
    registry = SubscriberRegistry(cfg.sqlite_path)
    row = registry.get(subscriber) or registry.get_by_name(subscriber)
    if not row:
        typer.echo(f"Subscriber {subscriber!r} not found.", err=True)
        raise typer.Exit(1)
    typer.echo(row.token_value)


@subscribers_app.command("revoke")
def subscribers_revoke(
    subscriber: Annotated[str, typer.Argument(help="Subscriber ID or name")],
):
    """Revoke a subscriber. Future pushes to them will be skipped immediately."""
    from ghost_daemon import config as _cfg_mod
    from ghost_daemon.subscribers.registry import SubscriberRegistry

    cfg = _cfg_mod.load()
    registry = SubscriberRegistry(cfg.sqlite_path)

    row = registry.get(subscriber) or registry.get_by_name(subscriber)
    if not row:
        typer.echo(f"Subscriber {subscriber!r} not found.", err=True)
        raise typer.Exit(1)

    registry.revoke(row.id)
    typer.echo(f"Subscriber '{row.name}' ({row.id}) revoked. Future pushes will be skipped.")


@app.command("share")
def share(
    draft_id: Annotated[str, typer.Argument(help="Draft ID to approve and share")],
    to: Annotated[str, typer.Option("--to", help="Subscriber ID or name")] = "",
):
    """Approve a draft and push it to a specific subscriber (or all active subscribers)."""
    from ghost_daemon import config as _cfg_mod
    from ghost_daemon.daemon import GhostDaemon
    from ghost_daemon.subscribers.registry import SubscriberRegistry
    from ghost_daemon.transport.push import push as _push
    from vantage.schemas import ApprovalSource

    cfg = _cfg_mod.load()
    daemon = GhostDaemon(cfg)
    registry = SubscriberRegistry(cfg.sqlite_path)

    row = daemon.store.get_draft(draft_id)
    if not row:
        typer.echo(f"Draft {draft_id!r} not found.", err=True)
        raise typer.Exit(1)

    if row.status == "rejected":
        typer.echo(f"Draft {draft_id!r} was rejected — cannot share.", err=True)
        raise typer.Exit(1)

    if row.status == "approved":
        approved = daemon.gate.get_approved(draft_id)
    else:
        approved = daemon.gate.approve(draft_id, source=ApprovalSource.MANUAL)

    targets = []
    if to:
        sub_row = registry.get(to) or registry.get_by_name(to)
        if not sub_row:
            typer.echo(f"Subscriber {to!r} not found.", err=True)
            raise typer.Exit(1)
        if sub_row.revoked_at:
            typer.echo(f"Subscriber {to!r} is revoked.", err=True)
            raise typer.Exit(1)
        targets = [sub_row]
    else:
        targets = registry.subscribers_for_channel(approved.artifact_type)
        if not targets:
            typer.echo("No active subscribers for this artifact type. Approve saved locally.")
            return

    async def _do_push():
        results = []
        for sub_row in targets:
            identity = registry.to_identity(sub_row)
            ok = await _push(approved, identity, cfg.developer_id)
            results.append((identity.name, ok))
        return results

    results = asyncio.run(_do_push())
    for name, ok in results:
        status = "OK" if ok else "FAILED"
        typer.echo(f"  → {name}: {status}")


@app.command("policy")
def policy_edit(
    subscriber: Annotated[str, typer.Argument(help="Subscriber ID or name")],
):
    """Open the sharing policy for a subscriber in your default editor."""
    import os
    import subprocess
    from ghost_daemon import config as _cfg_mod
    from ghost_daemon.subscribers.registry import SubscriberRegistry
    from ghost_daemon.approval.policy import load_policy, save_policy

    cfg = _cfg_mod.load()
    registry = SubscriberRegistry(cfg.sqlite_path)

    row = registry.get(subscriber) or registry.get_by_name(subscriber)
    if not row:
        typer.echo(f"Subscriber {subscriber!r} not found.", err=True)
        raise typer.Exit(1)

    policy = load_policy(row.id, cfg.vantage_dir)
    policy_path = save_policy(policy, cfg.vantage_dir)

    import sys
    editor = os.environ.get("EDITOR", "notepad" if sys.platform == "win32" else "nano")
    typer.echo(f"Opening policy for '{row.name}' in {editor}...")
    subprocess.call([editor, str(policy_path)])


@app.command("init")
def init_config():
    """Create a default config file at ~/.vantage/config.toml."""
    from ghost_daemon import config as _cfg_mod

    path = _cfg_mod.write_default()
    typer.echo(f"Config written to {path}")
    typer.echo("Edit it to set your developer_id, display_name, and LLM provider.")


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────

def _ensure_ui_running(cfg) -> None:
    """Start the UI server in the background if it isn't responding already."""
    import socket
    import threading

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.3)
    alive = sock.connect_ex((cfg.ui.bind_address, cfg.ui.port)) == 0
    sock.close()

    if alive:
        return

    from ghost_daemon.daemon import GhostDaemon
    import uvicorn

    daemon = GhostDaemon(cfg)
    uv_cfg = uvicorn.Config(
        app=daemon.get_app(),
        host=cfg.ui.bind_address,
        port=cfg.ui.port,
        log_level="warning",
    )
    server = uvicorn.Server(uv_cfg)

    def _run():
        asyncio.run(server.serve())

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    # Brief wait so the server is ready before the browser opens
    import time
    time.sleep(0.8)


def main():
    app()


if __name__ == "__main__":
    main()
