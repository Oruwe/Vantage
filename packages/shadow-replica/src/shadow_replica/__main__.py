from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(name="shadow-replica", help="Vantage Shadow Replica — subscriber-side receiver.")


@app.command("serve")
def serve(
    host: Annotated[str, typer.Option("--host")] = "0.0.0.0",
    port: Annotated[int, typer.Option("--port")] = 7475,
    config: Annotated[str, typer.Option("--config")] = "",
):
    """Start the shadow-replica receiver server."""
    import uvicorn
    from shadow_replica import config as _cfg_mod
    from shadow_replica.app import build_app

    cfg_path = Path(config) if config else None
    cfg = _cfg_mod.load(cfg_path)
    cfg.bind_host = host
    cfg.bind_port = port

    display_host = "localhost" if host in ("0.0.0.0", "127.0.0.1") else host
    typer.echo(f"Shadow replica starting on http://{host}:{port}")
    typer.echo(f"")
    typer.echo(f"  Manager dashboard : http://{display_host}:{port}/")
    typer.echo(f"  Manager token     : {cfg.manager_token}")
    typer.echo(f"")
    typer.echo(f"Trusted devs: {len(cfg.trusted_devs)}")
    for dev in cfg.trusted_devs:
        typer.echo(f"  • {dev.name} ({dev.developer_id})")

    if cfg.notifications:
        typer.echo(f"Notifications: {len(cfg.notifications)} configured")
        for n in cfg.notifications:
            label = n.url or n.host or "(no url)"
            typer.echo(f"  • {n.type}  {label}")
    else:
        typer.echo("Notifications: none (add [[notifications]] to replica.toml to enable)")

    uvicorn.run(build_app(cfg), host=host, port=port, log_level="info")


@app.command("import-dev")
def import_dev(
    developer_id: Annotated[str, typer.Argument(help="Developer ID from vantage-ghost subscribers list")],
    name: Annotated[str, typer.Option("--name", "-n")] = "",
    token: Annotated[str, typer.Option("--token", "-t")] = "",
    subscriber_id: Annotated[str, typer.Option("--subscriber-id", "-s", help="Subscriber ID printed by vantage-ghost subscribers add")] = "",
    config: Annotated[str, typer.Option("--config")] = "",
):
    """Register a dev as a trusted source. Run once with the token they gave you."""
    from shadow_replica import config as _cfg_mod

    if not token:
        token = typer.prompt("Enter the token provided by the developer")

    if not name:
        name = typer.prompt(f"Display name for developer {developer_id!r}", default=developer_id)

    cfg_path = Path(config) if config else None
    _cfg_mod.add_trusted_dev(
        developer_id=developer_id,
        name=name,
        token_value=token,
        subscriber_id=subscriber_id,
        path=cfg_path,
    )
    typer.echo(f"Developer {name!r} ({developer_id}) registered as trusted.")
    if subscriber_id:
        typer.echo(f"Subscriber ID: {subscriber_id} (used for live-probe policy lookup).")
    typer.echo("Run 'shadow-replica serve' to start accepting artifacts.")


@app.command("status")
def replica_status(
    config: Annotated[str, typer.Option("--config")] = "",
):
    """Show replica health: trusted devs, artifact counts, last received per dev."""
    from pathlib import Path as _Path
    from shadow_replica import config as _cfg_mod
    from shadow_replica.storage.received_store import ReceivedStore
    from shadow_replica.models.embedder import Embedder

    cfg_path = _Path(config) if config else None
    cfg = _cfg_mod.load(cfg_path)

    typer.echo(f"Shadow Replica — {cfg.replica_id[:12]}…")
    typer.echo("─" * 50)
    typer.echo(f"{'Bind':<18} {cfg.bind_host}:{cfg.bind_port}")
    typer.echo(f"{'Qdrant':<18} {'enabled' if cfg.qdrant.enabled else 'disabled'}  {cfg.qdrant.url}")
    typer.echo(f"{'Notifications':<18} {len(cfg.notifications)} configured")
    typer.echo()

    typer.echo(f"Trusted developers ({len(cfg.trusted_devs)})")
    if not cfg.trusted_devs:
        typer.echo("  (none — run 'shadow-replica import-dev' to add one)")
    else:
        embedder = Embedder(
            provider=cfg.embeddings.provider,
            model=cfg.embeddings.model,
            endpoint=cfg.embeddings.endpoint,
        ) if cfg.qdrant.enabled else None
        store = ReceivedStore(qdrant_url=cfg.qdrant.url, embedder=embedder)

        typer.echo(f"  {'NAME':<22} {'DEVELOPER ID':<36} ARTIFACTS  LAST RECEIVED")
        typer.echo("  " + "─" * 80)
        for dev in cfg.trusted_devs:
            count_rows = store.list_for_developer(dev.developer_id, limit=200)
            count = len(count_rows)
            rows = count_rows[:1]
            if rows:
                last = rows[0].received_at[:16].replace("T", " ")
                artifact_type = rows[0].artifact_type.replace("_", " ")
                last_label = f"{last} ({artifact_type})"
            else:
                last_label = "never"
            typer.echo(f"  {dev.name:<22} {dev.developer_id:<36} {count:<10} {last_label}")


@app.command("query")
def replica_query(
    query_text: Annotated[str, typer.Argument(help="Natural-language question about your developers")],
    developer: Annotated[str, typer.Option("--developer", "-d", help="Restrict to a specific developer ID or name")] = "",
    channel: Annotated[str, typer.Option("--channel", "-c", help="Filter by channel (weekly_digest, commit_report, …)")] = "",
    config: Annotated[str, typer.Option("--config")] = "",
):
    """Query stored artifacts using natural language.

    Examples:
      shadow-replica query "what has alice been working on?"
      shadow-replica query "recent commits" --developer alice
      shadow-replica query "blockers" --channel standup
    """
    import asyncio as _asyncio
    from pathlib import Path as _Path
    from shadow_replica import config as _cfg_mod
    from shadow_replica.storage.received_store import ReceivedStore
    from shadow_replica.models.embedder import Embedder
    from shadow_replica.agent.subscriber_agent import SubscriberAgent

    cfg_path = _Path(config) if config else None
    cfg = _cfg_mod.load(cfg_path)

    # Resolve developer name → developer_id
    dev_id: str | None = None
    if developer:
        matched = next(
            (d for d in cfg.trusted_devs
             if d.developer_id == developer or d.name.lower() == developer.lower()),
            None,
        )
        dev_id = matched.developer_id if matched else developer

    embedder = Embedder(
        provider=cfg.embeddings.provider,
        model=cfg.embeddings.model,
        endpoint=cfg.embeddings.endpoint,
    ) if cfg.qdrant.enabled else None

    store = ReceivedStore(qdrant_url=cfg.qdrant.url, embedder=embedder)

    # Build LLM wrapper the same way app.py does
    llm = None
    try:
        import os as _os
        from openai import AsyncOpenAI
        provider = cfg.llm.provider.lower()
        model = cfg.llm.model or "gpt-4o-mini"
        api_key = _os.environ.get(cfg.llm.api_key_env, "") if cfg.llm.api_key_env else ""

        endpoint_map = {
            "groq": "https://api.groq.com/openai/v1",
            "grok": "https://api.x.ai/v1",
            "openrouter": "https://openrouter.ai/api/v1",
        }
        if provider in endpoint_map or provider in ("openai_compat",):
            endpoint = cfg.llm.endpoint or endpoint_map.get(provider, "")
            client = AsyncOpenAI(base_url=endpoint, api_key=api_key or "x")

            class _Wrap:
                def __init__(self, c, m):
                    self._c, self._m = c, m
                async def complete(self, messages, max_tokens=500):
                    r = await self._c.chat.completions.create(model=self._m, messages=messages, max_tokens=max_tokens)
                    return r.choices[0].message.content or ""

            llm = _Wrap(client, model)
    except Exception:
        pass

    agent = SubscriberAgent(store, llm=llm, embedder=embedder)

    async def _run():
        return await agent.run_async(
            query_text,
            developer_id=dev_id,
            channel=channel or None,
        )

    result = _asyncio.run(_run())

    answer = result.get("answer", "")
    sources = result.get("sources", [])

    typer.echo(answer)
    if sources:
        typer.echo()
        typer.echo(f"Sources ({len(sources)}):")
        for s in sources[:5]:
            dev = s.get("developer_id", "")
            atype = s.get("artifact_type", "").replace("_", " ")
            when = s.get("received_at", s.get("approved_at", ""))[:10]
            typer.echo(f"  • {atype} from {dev} on {when}")


def main():
    app()


if __name__ == "__main__":
    main()
