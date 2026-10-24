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


def main():
    app()


if __name__ == "__main__":
    main()
