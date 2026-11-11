"""Approved artifacts and the bearer token must not cross the network in clear text.

Before this fix the replica could only serve plain HTTP and the ghost accepted
any ``http://`` subscriber URL — its own help text suggested one — so on a
shared network (campus Wi-Fi, office LAN) anyone could read every approved
artifact and copy the token.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from vantage.schemas import Channel


# ── Ghost: subscriber URLs ──────────────────────────────────────────────────

@pytest.fixture
def registry(tmp_path: Path):
    from ghost_daemon.subscribers.registry import SubscriberRegistry
    return SubscriberRegistry(tmp_path / "vantage.db")


@pytest.mark.parametrize("url", [
    "http://manager-host:7475",
    "http://10.0.0.5:7475",
    "ftp://manager-host",
    "manager-host:7475",
    "https://",
])
def test_insecure_or_malformed_urls_are_rejected(registry, url):
    with pytest.raises(ValueError):
        registry.add("mgr", url, [Channel.WEEKLY_DIGEST])
    assert registry.list_active() == []


@pytest.mark.parametrize("url", [
    "https://manager.example.com",
    "https://10.0.0.5:7475",
    "http://127.0.0.1:7475",
    "http://localhost:7475",
    "http://[::1]:7475",
])
def test_https_and_loopback_http_are_accepted(registry, url):
    identity = registry.add("mgr", url, [Channel.WEEKLY_DIGEST])
    assert identity.receive_url == url


def test_explicit_opt_in_allows_plain_http(registry):
    identity = registry.add(
        "mgr", "http://manager-host:7475", [Channel.WEEKLY_DIGEST], allow_insecure_http=True,
    )
    assert identity.receive_url == "http://manager-host:7475"


def test_cli_subscribers_add_refuses_plain_http(tmp_path: Path, monkeypatch):
    from ghost_daemon import __main__ as cli
    from ghost_daemon import config as cfg_mod

    monkeypatch.setenv("HOME", str(tmp_path))  # keep ~/.vantage inside the test dir
    monkeypatch.setattr(cfg_mod, "load", lambda *a, **k: cfg_mod.Config(developer_id="alice"))
    r = CliRunner().invoke(cli.app, ["subscribers", "add", "mgr", "-u", "http://manager-host:7475"])
    assert r.exit_code == 1
    assert "https://" in r.output
    assert "--allow-insecure-http" in r.output


# ── Replica: serving HTTPS ──────────────────────────────────────────────────

@pytest.fixture
def captured_run(monkeypatch):
    import uvicorn
    import shadow_replica.app as replica_app

    calls: list[dict] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append(kw))
    monkeypatch.setattr(replica_app, "build_app", lambda cfg: object())
    return calls


def test_replica_serve_passes_tls_files_to_uvicorn(tmp_path: Path, captured_run):
    from shadow_replica.__main__ import app

    key, cert = tmp_path / "key.pem", tmp_path / "cert.pem"
    key.write_text("k")
    cert.write_text("c")
    r = CliRunner().invoke(app, [
        "serve", "--config", str(tmp_path / "missing.toml"),
        "--ssl-keyfile", str(key), "--ssl-certfile", str(cert),
    ])
    assert r.exit_code == 0, r.output
    assert captured_run[0]["ssl_keyfile"] == str(key)
    assert captured_run[0]["ssl_certfile"] == str(cert)
    assert "https://" in r.output


def test_replica_serve_requires_both_tls_files(tmp_path: Path, captured_run):
    from shadow_replica.__main__ import app

    key = tmp_path / "key.pem"
    key.write_text("k")
    r = CliRunner().invoke(app, [
        "serve", "--config", str(tmp_path / "missing.toml"), "--ssl-keyfile", str(key),
    ])
    assert r.exit_code != 0
    assert captured_run == []


def test_replica_serve_without_tls_warns(tmp_path: Path, captured_run):
    from shadow_replica.__main__ import app

    r = CliRunner().invoke(app, ["serve", "--config", str(tmp_path / "missing.toml")])
    assert r.exit_code == 0, r.output
    assert "ssl_keyfile" not in captured_run[0]
    assert "WARNING" in r.output
