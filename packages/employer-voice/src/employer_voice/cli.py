"""Typer CLI for employer-voice (text chat frontend).

Commands:
  init           Write a starter config with the manager token + replica URL.
  config         Print the current config (token masked).
  ask MESSAGE    One text turn against the manager agent (optional --stream).
  conversations  List recent chats (ids + titles + turn counts).
  reset          Forget the saved conversation_id so the next turn starts fresh.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Optional

import typer

from employer_voice import config as _cfg

app = typer.Typer(name="employer-voice", help="Text chat frontend for the Vantage manager agent.")


def _resolve_config_path(custom: str) -> Optional[Path]:
    return Path(custom) if custom else None


@app.command("init")
def init(
    manager_token: Annotated[str, typer.Option("--manager-token", "-t", help="Manager token (vsm_...). Prompted if omitted.")] = "",
    replica_url: Annotated[str, typer.Option("--replica-url", "-u")] = "http://127.0.0.1:7475",
    config: Annotated[str, typer.Option("--config")] = "",
    force: Annotated[bool, typer.Option("--force/--no-force", help="Overwrite an existing config.")] = False,
) -> None:
    """Write a starter voice.toml."""
    path = _resolve_config_path(config)
    existing = _cfg.load(path)
    if existing.replica.manager_token and not force:
        typer.echo("A config already exists. Re-run with --force to overwrite.")
        raise typer.Exit(code=1)

    if not manager_token:
        manager_token = typer.prompt("Manager token (vsm_...)", hide_input=True)
    if not manager_token.strip():
        typer.echo("manager_token is required.")
        raise typer.Exit(code=1)

    cfg = _cfg.VoiceConfig(
        replica=_cfg.ReplicaTarget(url=replica_url, manager_token=manager_token.strip()),
    )
    written = _cfg.save(cfg, path)
    typer.echo(f"Wrote config to {written}")
    typer.echo(f"  Replica URL      : {cfg.replica.url}")
    typer.echo(f"  Manager token    : {_mask(cfg.replica.manager_token)}")


@app.command("config")
def show_config(
    config: Annotated[str, typer.Option("--config")] = "",
) -> None:
    """Print the current config (manager token redacted)."""
    cfg = _cfg.load(_resolve_config_path(config))
    typer.echo(f"Replica URL      : {cfg.replica.url}")
    typer.echo(f"Manager token    : {_mask(cfg.replica.manager_token)}")
    if cfg.conversation_id:
        typer.echo(f"Saved convo id   : {cfg.conversation_id}")


@app.command("ask")
def ask(
    message: Annotated[str, typer.Argument(help="Message to send to the manager agent.")],
    stream: Annotated[bool, typer.Option("--stream/--no-stream", help="Show tool calls as they run.")] = False,
    auto_approve: Annotated[bool, typer.Option("--auto-approve", help="Approve every dangerous-tool prompt without asking. For scripts/CI.")] = False,
    deny_all: Annotated[bool, typer.Option("--deny-all", help="Deny every dangerous-tool prompt without asking. Safe default for unattended runs.")] = False,
    config: Annotated[str, typer.Option("--config")] = "",
) -> None:
    """One text turn against the manager agent.

    If the agent pauses on a dangerous tool (requires_confirmation=True),
    the stream mode prompts the manager for yes/no and resumes. Use
    ``--auto-approve`` or ``--deny-all`` to bypass the prompt in scripts.
    """
    if auto_approve and deny_all:
        typer.echo("Error: --auto-approve and --deny-all are mutually exclusive.", err=True)
        raise typer.Exit(code=1)
    policy: Optional[bool] = True if auto_approve else (False if deny_all else None)

    path = _resolve_config_path(config)
    from employer_voice import session as _sess

    try:
        if stream:
            summary = asyncio.run(_run_stream_with_approval(message, path, policy))
            if summary.assistant_text:
                typer.echo("")
                typer.echo(summary.assistant_text)
        else:
            result = asyncio.run(_sess.ask_once(message, config_path=path))
            typer.echo(result.assistant_message)
            if result.tool_calls:
                typer.echo("")
                typer.echo(f"({len(result.tool_calls)} tool call(s) in {result.loops} loop(s))")
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)


async def _run_stream_with_approval(
    message: str,
    config_path: Optional[Path],
    policy: Optional[bool],
) -> "object":
    """Run ask_stream then loop through pending approvals until the turn ends.

    ``policy`` is True to auto-approve, False to auto-deny, or None to prompt.
    Returns the final StreamSummary (post-resume chain).
    """
    from employer_voice import session as _sess

    summary = await _sess.ask_stream(
        message, config_path=config_path, on_event=_print_stream_event,
    )
    # Guard against unbounded resume chains if the agent keeps requesting
    # dangerous tools. Matches the dashboard's UX (one approval per card).
    safety = 0
    while summary.pending_approval and summary.pending_tool_use_id:
        safety += 1
        if safety > 8:
            typer.echo("  [stopped — too many consecutive approval requests]", err=True)
            break
        decision = _resolve_approval(summary, policy)
        typer.echo("")
        typer.echo(f"  [{'approved' if decision else 'denied'}] → resuming…")
        summary = await _sess.resume_approval(
            tool_use_id=summary.pending_tool_use_id,
            approved=decision,
            config_path=config_path,
            on_event=_print_stream_event,
        )
    return summary


def _resolve_approval(summary, policy: Optional[bool]) -> bool:
    tool = summary.pending_tool_name or "?"
    args = summary.pending_tool_args or {}
    typer.echo("")
    typer.echo(f"  ⚠ Agent wants to run: {tool}({_short_args(args)})")
    if policy is True:
        typer.echo("  (--auto-approve) → yes")
        return True
    if policy is False:
        typer.echo("  (--deny-all) → no")
        return False
    return typer.confirm("  Approve?", default=False)


@app.command("conversations")
def conversations(
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
    config: Annotated[str, typer.Option("--config")] = "",
) -> None:
    """List recent chats stored on the replica."""
    path = _resolve_config_path(config)
    cfg = _cfg.load(path)
    from employer_voice.replica_client import ReplicaClient

    if not cfg.replica.manager_token:
        typer.echo("No manager_token configured. Run 'employer-voice init' first.", err=True)
        raise typer.Exit(code=1)

    client = ReplicaClient(cfg.replica.url, cfg.replica.manager_token)
    try:
        convos = asyncio.run(client.list_conversations(limit=limit))
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)

    if not convos:
        typer.echo("(no conversations yet)")
        return
    for c in convos:
        cid = c.get("conversation_id", "")
        title = c.get("title", "") or "(untitled)"
        turns = c.get("turn_count", "?")
        updated = c.get("updated_at", "")
        marker = "*" if cid == cfg.conversation_id else " "
        typer.echo(f"{marker} {cid[:8]}  turns={turns:>3}  {title[:60]}  {updated}")


@app.command("reset")
def reset(
    config: Annotated[str, typer.Option("--config")] = "",
) -> None:
    """Forget the saved conversation_id so the next ask starts fresh."""
    path = _resolve_config_path(config)
    cfg = _cfg.load(path)
    if not cfg.conversation_id:
        typer.echo("No saved conversation_id to forget.")
        return
    cfg.conversation_id = ""
    _cfg.save(cfg, path)
    typer.echo("Cleared saved conversation_id.")


def _mask(token: str) -> str:
    if not token:
        return "(unset)"
    if len(token) <= 8:
        return "****"
    return f"{token[:6]}…{token[-4:]}"


def _print_stream_event(ev: dict) -> None:
    name = ev.get("event")
    if name == "tool_call":
        tool = ev.get("tool", "?")
        args = ev.get("args", {})
        pending = " (needs approval)" if ev.get("requires_confirmation") else ""
        typer.echo(f"  → {tool}({_short_args(args)}){pending}")
    elif name == "tool_result":
        latency = ev.get("latency_ms", 0)
        preview = (ev.get("preview", "") or "")[:80]
        typer.echo(f"    {latency}ms  {preview}")
    elif name == "message_delta":
        typer.echo(ev.get("content", ""), nl=False)
    elif name == "done" and ev.get("budget_exhausted"):
        typer.echo("  [budget exhausted]")


def _short_args(args: dict) -> str:
    if not args:
        return ""
    items = [f"{k}={_short_val(v)}" for k, v in list(args.items())[:3]]
    return ", ".join(items)


def _short_val(v) -> str:
    s = repr(v)
    return s if len(s) <= 40 else s[:37] + "…"


__all__ = ["app"]
