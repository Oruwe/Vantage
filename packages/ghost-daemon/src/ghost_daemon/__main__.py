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

    agent = DigestAgent(cfg, daemon.llm, daemon.project_map, daemon.tailer, qdrant=daemon.qdrant)

    async def _run():
        draft = await agent.run(period_start, period_end, repo_filter=repo or None)
        draft_id = daemon.gate.submit(draft)
        return draft_id

    draft_id = asyncio.run(_run())
    url = f"http://{cfg.ui.bind_address}:{cfg.ui.port}/draft/{draft_id}"
    typer.echo(f"Draft ready: {url}")

    if open_ui:
        _open_or_start_ui(url, cfg, daemon)


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
        daemon = GhostDaemon(cfg)
        _open_or_start_ui(url, cfg, daemon)


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
    daemon = GhostDaemon(cfg)
    _open_or_start_ui(url, cfg, daemon)


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
    typer.echo(registry.to_identity(row).token_value)


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


@subscribers_app.command("rotate")
def subscribers_rotate(
    subscriber: Annotated[str, typer.Argument(help="Subscriber ID or name")],
):
    """Rotate the bearer token for a subscriber. Old token is immediately invalidated."""
    from ghost_daemon import config as _cfg_mod
    from ghost_daemon.subscribers.registry import SubscriberRegistry

    cfg = _cfg_mod.load()
    registry = SubscriberRegistry(cfg.sqlite_path)

    row = registry.get(subscriber) or registry.get_by_name(subscriber)
    if not row:
        typer.echo(f"Subscriber {subscriber!r} not found.", err=True)
        raise typer.Exit(1)
    if row.revoked_at:
        typer.echo(f"Subscriber '{row.name}' is already revoked.", err=True)
        raise typer.Exit(1)

    new_token = registry.rotate(row.id)
    typer.echo(f"Token rotated for '{row.name}' ({row.id}).")
    typer.echo(f"\nNew token (give this to {row.name} — old token is now invalid):")
    typer.echo(f"  {new_token}")
    typer.echo(f"\nThey should update their shadow-replica with:")
    typer.echo(f"  shadow-replica import-dev {cfg.developer_id} --name \"{row.name}\" --token {new_token}")


@subscribers_app.command("token-history")
def subscribers_token_history(
    subscriber: Annotated[str, typer.Argument(help="Subscriber ID or name")],
):
    """Show the full token issuance and revocation history for a subscriber."""
    from ghost_daemon import config as _cfg_mod
    from ghost_daemon.subscribers.registry import SubscriberRegistry

    cfg = _cfg_mod.load()
    registry = SubscriberRegistry(cfg.sqlite_path)

    row = registry.get(subscriber) or registry.get_by_name(subscriber)
    if not row:
        typer.echo(f"Subscriber {subscriber!r} not found.", err=True)
        raise typer.Exit(1)

    history = registry.token_history(row.id)
    if not history:
        typer.echo(f"No token history for '{row.name}'.")
        return

    typer.echo(f"Token history for '{row.name}' ({row.id}) — {len(history)} entr{'y' if len(history)==1 else 'ies'}:")
    typer.echo(f"{'#':<4} {'ISSUED':<26} {'REVOKED':<26} {'REASON'}")
    typer.echo("-" * 80)
    for i, h in enumerate(history, 1):
        revoked = h.revoked_at or "—"
        typer.echo(f"{i:<4} {h.issued_at:<26} {revoked:<26} {h.revocation_reason}")


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
        any_ok = False
        for sub_row in targets:
            identity = registry.to_identity(sub_row)
            ok = await _push(approved, identity, cfg.developer_id)
            results.append((identity.name, ok))
            if ok:
                any_ok = True
        if any_ok:
            await daemon.index_approved(approved)
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


@app.command("commit-report")
def commit_report(
    repo: Annotated[str, typer.Option("--repo", "-r", help="Path to the git repo (default: cwd)")] = "",
    sha: Annotated[str, typer.Option("--sha", help="Commit SHA (default: HEAD)")] = "",
    open_ui: Annotated[bool, typer.Option("--open/--no-open")] = False,
):
    """Generate a commit report draft from the latest (or specified) commit."""
    from ghost_daemon import config as _cfg_mod
    from ghost_daemon.logging_setup import setup_logging
    from ghost_daemon.daemon import GhostDaemon
    from ghost_daemon.agents.commit_agent import CommitAgent
    import os

    cfg = _cfg_mod.load()
    setup_logging(cfg.log_dir, cfg.log_level)
    daemon = GhostDaemon(cfg)

    repo_path = Path(repo).resolve() if repo else Path(os.getcwd())
    agent = CommitAgent(cfg, daemon.llm, daemon.tailer, qdrant=daemon.qdrant)

    async def _run():
        draft = await agent.run(repo_path, commit_sha=sha or None)
        return daemon.gate.submit(draft)

    draft_id = asyncio.run(_run())
    url = f"http://{cfg.ui.bind_address}:{cfg.ui.port}/draft/{draft_id}"
    typer.echo(f"Commit report draft ready: {url}")

    if open_ui:
        _open_or_start_ui(url, cfg, daemon)


@app.command("pr-draft")
def pr_draft(
    repo: Annotated[str, typer.Option("--repo", "-r", help="Path to the git repo (default: cwd)")] = "",
    base: Annotated[str, typer.Option("--base", "-b", help="Base branch to diff against")] = "main",
    open_ui: Annotated[bool, typer.Option("--open/--no-open")] = True,
):
    """Generate a PR description draft from the current branch's history."""
    from ghost_daemon import config as _cfg_mod
    from ghost_daemon.logging_setup import setup_logging
    from ghost_daemon.daemon import GhostDaemon
    from ghost_daemon.agents.pr_agent import PRAgent
    import os

    cfg = _cfg_mod.load()
    setup_logging(cfg.log_dir, cfg.log_level)
    daemon = GhostDaemon(cfg)

    repo_path = Path(repo).resolve() if repo else Path(os.getcwd())
    agent = PRAgent(cfg, daemon.llm, daemon.tailer)

    async def _run():
        draft = await agent.run(repo_path, base_branch=base)
        return daemon.gate.submit(draft)

    typer.echo(f"Generating PR draft for current branch → {base} …")
    draft_id = asyncio.run(_run())
    url = f"http://{cfg.ui.bind_address}:{cfg.ui.port}/draft/{draft_id}"
    typer.echo(f"Draft ready: {url}")

    if open_ui:
        _open_or_start_ui(url, cfg, daemon)


@app.command("standup")
def standup(
    since: Annotated[str, typer.Option(help="Period start (default: yesterday)")] = "yesterday",
    open_ui: Annotated[bool, typer.Option("--open/--no-open")] = True,
):
    """Generate standup bullet points from yesterday's session activity."""
    from ghost_daemon import config as _cfg_mod
    from ghost_daemon.logging_setup import setup_logging
    from ghost_daemon.daemon import GhostDaemon
    from ghost_daemon.agents.standup_agent import StandupAgent

    cfg = _cfg_mod.load()
    setup_logging(cfg.log_dir, cfg.log_level)
    daemon = GhostDaemon(cfg)

    since_dt = _parse_since(since)
    agent = StandupAgent(cfg, daemon.llm, daemon.project_map, daemon.tailer)

    async def _run():
        draft = await agent.run(since=since_dt)
        return daemon.gate.submit(draft)

    typer.echo("Generating standup bullets …")
    draft_id = asyncio.run(_run())
    url = f"http://{cfg.ui.bind_address}:{cfg.ui.port}/draft/{draft_id}"
    typer.echo(f"Draft ready: {url}")

    if open_ui:
        _open_or_start_ui(url, cfg, daemon)


hook_app = typer.Typer(help="Manage the Vantage git post-commit hook.")
app.add_typer(hook_app, name="hook")


@hook_app.command("install")
def hook_install(
    repo: Annotated[str, typer.Argument(help="Path to git repo (default: cwd)")] = "",
):
    """Install the Vantage post-commit hook in a git repository."""
    from ghost_daemon.watchers.git_hook import install, is_installed
    import os

    repo_path = Path(repo).resolve() if repo else Path(os.getcwd())
    if not (repo_path / ".git").exists():
        typer.echo(f"Not a git repository: {repo_path}", err=True)
        raise typer.Exit(1)

    if is_installed(repo_path):
        typer.echo(f"Hook already installed in {repo_path / '.git' / 'hooks' / 'post-commit'}")
        return

    hook_path = install(repo_path)
    typer.echo(f"Hook installed: {hook_path}")
    typer.echo("After each git commit, a draft commit report will be enqueued in 'vantage-ghost review'.")


@hook_app.command("status")
def hook_status(
    repo: Annotated[str, typer.Argument(help="Path to git repo (default: cwd)")] = "",
):
    """Check whether the Vantage post-commit hook is installed."""
    from ghost_daemon.watchers.git_hook import is_installed
    import os

    repo_path = Path(repo).resolve() if repo else Path(os.getcwd())
    installed = is_installed(repo_path)
    status = "installed" if installed else "not installed"
    typer.echo(f"Vantage post-commit hook: {status} ({repo_path})")


@hook_app.command("uninstall")
def hook_uninstall(
    repo: Annotated[str, typer.Argument(help="Path to git repo (default: cwd)")] = "",
):
    """Remove the Vantage post-commit hook from a git repository."""
    from ghost_daemon.watchers.git_hook import is_installed
    import os

    repo_path = Path(repo).resolve() if repo else Path(os.getcwd())
    hook_path = repo_path / ".git" / "hooks" / "post-commit"
    if not hook_path.exists():
        typer.echo("No post-commit hook found.")
        return
    if not is_installed(repo_path):
        typer.echo("The existing post-commit hook was not installed by Vantage — leaving it untouched.")
        return
    hook_path.unlink()
    typer.echo(f"Vantage post-commit hook removed from {hook_path}")


service_app = typer.Typer(help="Install or remove the Vantage background service.")
app.add_typer(service_app, name="service")


@service_app.command("install")
def service_install(
    mode: Annotated[str, typer.Option(help="Installer type: auto, windows, systemd, launchd")] = "auto",
):
    """Register vantage-ghost run as a background service / scheduled task."""
    import sys
    import shutil

    resolved = mode
    if mode == "auto":
        if sys.platform == "win32":
            resolved = "windows"
        elif sys.platform == "darwin":
            resolved = "launchd"
        else:
            resolved = "systemd"

    if resolved == "windows":
        _service_install_windows()
    elif resolved == "systemd":
        _service_install_systemd()
    elif resolved == "launchd":
        _service_install_launchd()
    else:
        typer.echo(f"Unknown mode {resolved!r}. Choose: windows, systemd, launchd.", err=True)
        raise typer.Exit(1)


@service_app.command("uninstall")
def service_uninstall(
    mode: Annotated[str, typer.Option(help="Installer type: auto, windows, systemd, launchd")] = "auto",
):
    """Remove the Vantage background service registration."""
    import sys

    resolved = mode
    if mode == "auto":
        if sys.platform == "win32":
            resolved = "windows"
        elif sys.platform == "darwin":
            resolved = "launchd"
        else:
            resolved = "systemd"

    if resolved == "windows":
        _service_uninstall_windows()
    elif resolved == "systemd":
        _service_uninstall_systemd()
    elif resolved == "launchd":
        _service_uninstall_launchd()
    else:
        typer.echo(f"Unknown mode {resolved!r}. Choose: windows, systemd, launchd.", err=True)
        raise typer.Exit(1)


@service_app.command("status")
def service_status():
    """Check whether the Vantage service is installed and running."""
    import sys
    import subprocess

    if sys.platform == "win32":
        import winreg
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY, 0, winreg.KEY_READ)
            value, _ = winreg.QueryValueEx(key, _WIN_VALUE_NAME)
            winreg.CloseKey(key)
            typer.echo(f"Vantage daemon: registered in HKCU Run key.")
            typer.echo(f"  Command: {value}")
        except FileNotFoundError:
            typer.echo("Vantage daemon: not registered (use 'vantage-ghost service install').")
    elif sys.platform == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / "com.vantage.ghost.plist"
        installed = plist.exists()
        typer.echo(f"launchd plist {'installed' if installed else 'not found'}: {plist}")
    else:
        unit = Path.home() / ".config" / "systemd" / "user" / "vantage.service"
        installed = unit.exists()
        typer.echo(f"systemd unit {'installed' if installed else 'not found'}: {unit}")
        if installed:
            import subprocess
            r = subprocess.run(
                ["systemctl", "--user", "is-active", "vantage.service"],
                capture_output=True, text=True,
            )
            typer.echo(f"  status: {r.stdout.strip()}")


_WIN_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_WIN_VALUE_NAME = "VantageGhostDaemon"


def _service_install_windows() -> None:
    import shutil
    import winreg

    vantage_exe = shutil.which("vantage-ghost") or shutil.which("vantage")
    if not vantage_exe:
        typer.echo("Could not find vantage-ghost on PATH.", err=True)
        raise typer.Exit(1)

    command = f'"{vantage_exe}" run'
    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        _WIN_RUN_KEY,
        0,
        winreg.KEY_SET_VALUE,
    )
    winreg.SetValueEx(key, _WIN_VALUE_NAME, 0, winreg.REG_SZ, command)
    winreg.CloseKey(key)

    typer.echo("Vantage daemon registered in HKCU Run key.")
    typer.echo("It will start automatically at your next login.")
    typer.echo(f"Command: {command}")


def _service_uninstall_windows() -> None:
    import winreg

    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            _WIN_RUN_KEY,
            0,
            winreg.KEY_SET_VALUE,
        )
        winreg.DeleteValue(key, _WIN_VALUE_NAME)
        winreg.CloseKey(key)
        typer.echo("Vantage daemon removed from HKCU Run key.")
    except FileNotFoundError:
        typer.echo("Vantage Run key entry not found — nothing to remove.")


def _service_install_systemd() -> None:
    import shutil as _shutil
    exe = _shutil.which("vantage-ghost") or _shutil.which("vantage")
    if not exe:
        typer.echo("vantage-ghost not found on PATH.", err=True)
        raise typer.Exit(1)

    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = unit_dir / "vantage.service"
    unit_path.write_text(
        f"[Unit]\nDescription=Vantage Ghost Daemon\nAfter=graphical-session.target\n\n"
        f"[Service]\nExecStart={exe} run\nRestart=on-failure\nRestartSec=5\n\n"
        f"[Install]\nWantedBy=default.target\n",
        encoding="utf-8",
    )
    import subprocess
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    subprocess.run(["systemctl", "--user", "enable", "--now", "vantage.service"], check=False)
    typer.echo(f"systemd user service installed: {unit_path}")
    typer.echo("It will start automatically at login. Running now.")


def _service_uninstall_systemd() -> None:
    import subprocess
    subprocess.run(["systemctl", "--user", "disable", "--now", "vantage.service"], check=False)
    unit_path = Path.home() / ".config" / "systemd" / "user" / "vantage.service"
    if unit_path.exists():
        unit_path.unlink()
        typer.echo(f"Removed: {unit_path}")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    typer.echo("systemd user service uninstalled.")


def _service_install_launchd() -> None:
    import shutil as _shutil
    exe = _shutil.which("vantage-ghost") or _shutil.which("vantage")
    if not exe:
        typer.echo("vantage-ghost not found on PATH.", err=True)
        raise typer.Exit(1)

    agents_dir = Path.home() / "Library" / "LaunchAgents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    plist_path = agents_dir / "com.vantage.ghost.plist"
    plist_path.write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.vantage.ghost</string>
  <key>ProgramArguments</key>
  <array><string>{exe}</string><string>run</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>""",
        encoding="utf-8",
    )
    import subprocess
    subprocess.run(["launchctl", "load", str(plist_path)], check=False)
    typer.echo(f"launchd agent installed: {plist_path}")
    typer.echo("The daemon will start now and at every login.")


def _service_uninstall_launchd() -> None:
    import subprocess
    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.vantage.ghost.plist"
    if plist_path.exists():
        subprocess.run(["launchctl", "unload", str(plist_path)], check=False)
        plist_path.unlink()
        typer.echo(f"Removed: {plist_path}")
    else:
        typer.echo("launchd plist not found — nothing to remove.")


@app.command("update-check")
def update_check():
    """Check PyPI for a newer version of vantage-ghost."""
    from importlib.metadata import version as _pkg_version, PackageNotFoundError

    try:
        current = _pkg_version("ghost-daemon")
    except PackageNotFoundError:
        current = None

    try:
        import httpx
        resp = httpx.get("https://pypi.org/pypi/ghost-daemon/json", timeout=5.0)
        resp.raise_for_status()
        latest = resp.json()["info"]["version"]
    except Exception as exc:
        typer.echo(f"Could not reach PyPI: {exc}", err=True)
        return

    if current is None:
        typer.echo(f"Latest release: v{latest}")
        typer.echo("(Running from source — installed version unknown)")
        return

    def _ver(v: str) -> tuple:
        try:
            return tuple(int(x) for x in v.split("."))
        except ValueError:
            return (0,)

    if _ver(latest) > _ver(current):
        typer.echo(f"Update available: v{current} → v{latest}")
        typer.echo("Run: pip install --upgrade ghost-daemon")
    else:
        typer.echo(f"vantage-ghost is up to date (v{current})")


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

def _open_or_start_ui(url: str, cfg, daemon) -> None:
    """Open the browser; start the UI server only if it isn't already listening."""
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.3)
    already_running = sock.connect_ex((cfg.ui.bind_address, cfg.ui.port)) == 0
    sock.close()

    webbrowser.open(url)

    if not already_running:
        import uvicorn as _uvicorn
        uv_cfg = _uvicorn.Config(
            app=daemon.get_app(),
            host=cfg.ui.bind_address,
            port=cfg.ui.port,
            log_level="warning",
        )
        server = _uvicorn.Server(uv_cfg)
        typer.echo("Review UI running — press Ctrl+C to stop.")
        asyncio.run(server.serve())


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
