"""Tests for the employer-voice (text chat) package.

Covers:
  - Config round-trip (defaults + persistence + conversation_id update)
  - ReplicaClient.chat / chat_stream against a mocked httpx transport
  - ReplicaClient.list_conversations
  - SSE parser handles multi-line data + comments
  - CLI `init` writes config, `ask` prints assistant text, `conversations`
    renders a list, `reset` clears the saved id
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from typer.testing import CliRunner


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_cfg_path(tmp_path: Path) -> Path:
    return tmp_path / "voice.toml"


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


# ── Config ──────────────────────────────────────────────────────────────────


def test_config_defaults_when_missing(tmp_cfg_path: Path) -> None:
    from employer_voice import config as cfgmod

    cfg = cfgmod.load(tmp_cfg_path)
    assert cfg.replica.url.startswith("http")
    assert cfg.replica.manager_token == ""
    assert cfg.conversation_id == ""


def test_config_roundtrip(tmp_cfg_path: Path) -> None:
    from employer_voice import config as cfgmod

    cfg = cfgmod.VoiceConfig(
        replica=cfgmod.ReplicaTarget(url="http://example:7475", manager_token="vsm_abc"),
        conversation_id="conv-xyz",
    )
    cfgmod.save(cfg, tmp_cfg_path)
    assert tmp_cfg_path.exists()

    loaded = cfgmod.load(tmp_cfg_path)
    assert loaded.replica.url == "http://example:7475"
    assert loaded.replica.manager_token == "vsm_abc"
    assert loaded.conversation_id == "conv-xyz"


def test_update_conversation_id_persists(tmp_cfg_path: Path) -> None:
    from employer_voice import config as cfgmod

    cfgmod.save(cfgmod.VoiceConfig(replica=cfgmod.ReplicaTarget(manager_token="vsm_x")), tmp_cfg_path)
    cfgmod.update_conversation_id("new-id", tmp_cfg_path)
    assert cfgmod.load(tmp_cfg_path).conversation_id == "new-id"


# ── Replica client ──────────────────────────────────────────────────────────


def _mock_transport(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)


def test_replica_client_requires_token() -> None:
    from employer_voice.replica_client import ReplicaClient

    with pytest.raises(ValueError):
        ReplicaClient("http://127.0.0.1", "")


def test_chat_posts_bearer_and_conversation_id() -> None:
    from employer_voice.replica_client import ReplicaClient

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={
            "conversation_id": "conv-1",
            "assistant_message": "hi there",
            "tool_calls": [{"tool_use_id": "t1", "tool_name": "list_developers", "tool_input": {},
                            "tool_output_preview": "{}", "latency_ms": 42}],
            "stop_reason": "end_turn",
            "loops": 1,
            "model": "claude-sonnet-4-6",
            "budget_exhausted": False,
        })

    async def _go():
        async with _mock_transport(handler) as c:
            cli = ReplicaClient("http://localhost:7475", "vsm_test")
            result = await cli.chat("hello", conversation_id="prev-1", client=c)
            return result

    result = asyncio.run(_go())
    assert result.conversation_id == "conv-1"
    assert result.assistant_message == "hi there"
    assert result.tool_calls and result.tool_calls[0]["tool_name"] == "list_developers"
    assert captured["url"].endswith("/ui/api/chat")
    assert captured["headers"]["authorization"] == "Bearer vsm_test"
    assert captured["body"] == {"message": "hello", "conversation_id": "prev-1"}


def test_chat_non_2xx_raises_replica_error() -> None:
    from employer_voice.replica_client import ReplicaClient, ReplicaError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="nope")

    async def _go():
        async with _mock_transport(handler) as c:
            cli = ReplicaClient("http://localhost:7475", "vsm_bad")
            await cli.chat("hi", client=c)

    with pytest.raises(ReplicaError):
        asyncio.run(_go())


def test_chat_stream_parses_sse_rounds() -> None:
    """Stream must yield started → tool_call → tool_result → message_delta → done."""
    from employer_voice.replica_client import ReplicaClient

    events = [
        ("started", {"conversation_id": "conv-7"}),
        ("tool_call", {"id": "t1", "tool": "list_developers", "args": {}, "requires_confirmation": False}),
        ("tool_result", {"id": "t1", "tool": "list_developers", "preview": "{}", "latency_ms": 11, "approved": True}),
        ("message_delta", {"content": "Alice is active."}),
        ("done", {"conversation_id": "conv-7", "loops": 1, "stop_reason": "end_turn",
                  "budget_exhausted": False, "pending_approval": False}),
    ]
    body = ""
    for name, payload in events:
        body += f"event: {name}\ndata: {json.dumps(payload)}\n\n"
    # Add a stray comment + empty event to exercise the parser
    body = ":heartbeat\n\n" + body

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    async def _go():
        async with _mock_transport(handler) as c:
            cli = ReplicaClient("http://localhost:7475", "vsm_test")
            out: list[dict] = []
            async for ev in cli.chat_stream("hi", client=c):
                out.append(ev)
            return out

    out = asyncio.run(_go())
    names = [e["event"] for e in out]
    assert names == ["started", "tool_call", "tool_result", "message_delta", "done"]
    assert out[0]["conversation_id"] == "conv-7"
    assert out[3]["content"] == "Alice is active."


def test_chat_stream_multiline_data_is_rejoined() -> None:
    """A single SSE event with two data: lines should decode as one JSON blob."""
    from employer_voice.replica_client import ReplicaClient

    body = (
        "event: done\n"
        "data: {\"conversation_id\":\n"
        "data:  \"conv-ml\", \"loops\": 2}\n"
        "\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    async def _go():
        async with _mock_transport(handler) as c:
            cli = ReplicaClient("http://localhost:7475", "vsm_test")
            events = [ev async for ev in cli.chat_stream("hi", client=c)]
            return events

    events = asyncio.run(_go())
    assert len(events) == 1
    assert events[0]["event"] == "done"
    assert events[0]["conversation_id"] == "conv-ml"
    assert events[0]["loops"] == 2


def test_list_conversations_passes_limit() -> None:
    from employer_voice.replica_client import ReplicaClient

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"conversations": [
            {"conversation_id": "a", "title": "First", "turn_count": 3, "updated_at": "2026-10-26T00:00:00Z"},
        ]})

    async def _go():
        async with _mock_transport(handler) as c:
            cli = ReplicaClient("http://localhost:7475", "vsm_test")
            return await cli.list_conversations(limit=5, client=c)

    convos = asyncio.run(_go())
    assert len(convos) == 1 and convos[0]["conversation_id"] == "a"
    assert "limit=5" in captured["url"]


# ── Session orchestrator ────────────────────────────────────────────────────


def test_ask_once_persists_conversation_id(tmp_cfg_path: Path) -> None:
    from employer_voice import config as cfgmod
    from employer_voice import session as sess
    from employer_voice.replica_client import ChatResult

    cfgmod.save(cfgmod.VoiceConfig(
        replica=cfgmod.ReplicaTarget(url="http://x", manager_token="vsm_t"),
    ), tmp_cfg_path)

    fake_result = ChatResult(
        conversation_id="conv-99",
        assistant_message="hi",
        tool_calls=[],
        stop_reason="end_turn",
        loops=1,
        model="claude-sonnet-4-6",
        budget_exhausted=False,
    )

    async def _fake_chat(self, message, conversation_id=None, *, client=None):
        return fake_result

    with patch("employer_voice.replica_client.ReplicaClient.chat", _fake_chat):
        result = asyncio.run(sess.ask_once("hello", config_path=tmp_cfg_path))
    assert result.conversation_id == "conv-99"
    assert cfgmod.load(tmp_cfg_path).conversation_id == "conv-99"


def test_ask_stream_collects_message_deltas(tmp_cfg_path: Path) -> None:
    from employer_voice import config as cfgmod
    from employer_voice import session as sess

    cfgmod.save(cfgmod.VoiceConfig(
        replica=cfgmod.ReplicaTarget(url="http://x", manager_token="vsm_t"),
    ), tmp_cfg_path)

    async def _fake_stream(self, message, conversation_id=None, *, client=None):
        for ev in [
            {"event": "started", "conversation_id": "conv-stream"},
            {"event": "message_delta", "content": "Hello "},
            {"event": "message_delta", "content": "world."},
            {"event": "done", "conversation_id": "conv-stream", "loops": 1, "stop_reason": "end_turn",
             "budget_exhausted": False, "pending_approval": False},
        ]:
            yield ev

    with patch("employer_voice.replica_client.ReplicaClient.chat_stream", _fake_stream):
        events_seen: list[dict] = []
        summary = asyncio.run(sess.ask_stream(
            "hi", config_path=tmp_cfg_path, on_event=events_seen.append,
        ))
    assert summary.assistant_text == "Hello world."
    assert summary.conversation_id == "conv-stream"
    assert cfgmod.load(tmp_cfg_path).conversation_id == "conv-stream"
    assert [e["event"] for e in events_seen] == ["started", "message_delta", "message_delta", "done"]


def test_ask_stream_error_event_raises(tmp_cfg_path: Path) -> None:
    from employer_voice import config as cfgmod
    from employer_voice import session as sess

    cfgmod.save(cfgmod.VoiceConfig(
        replica=cfgmod.ReplicaTarget(url="http://x", manager_token="vsm_t"),
    ), tmp_cfg_path)

    async def _fake_stream(self, message, conversation_id=None, *, client=None):
        yield {"event": "started", "conversation_id": "conv-err"}
        yield {"event": "error", "detail": "boom"}

    with patch("employer_voice.replica_client.ReplicaClient.chat_stream", _fake_stream):
        with pytest.raises(RuntimeError, match="boom"):
            asyncio.run(sess.ask_stream("hi", config_path=tmp_cfg_path))


def test_ask_once_without_token_raises(tmp_cfg_path: Path) -> None:
    from employer_voice import session as sess

    with pytest.raises(RuntimeError, match="manager_token"):
        asyncio.run(sess.ask_once("hi", config_path=tmp_cfg_path))


# ── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_init_writes_config(cli_runner: CliRunner, tmp_cfg_path: Path) -> None:
    from employer_voice.cli import app

    result = cli_runner.invoke(app, [
        "init",
        "--manager-token", "vsm_fromcli",
        "--replica-url", "http://127.0.0.1:7475",
        "--config", str(tmp_cfg_path),
    ])
    assert result.exit_code == 0, result.output
    assert "Wrote config" in result.output

    from employer_voice import config as cfgmod
    cfg = cfgmod.load(tmp_cfg_path)
    assert cfg.replica.manager_token == "vsm_fromcli"
    assert cfg.replica.url == "http://127.0.0.1:7475"


def test_cli_init_refuses_overwrite_without_force(cli_runner: CliRunner, tmp_cfg_path: Path) -> None:
    from employer_voice.cli import app

    cli_runner.invoke(app, ["init", "-t", "vsm_a", "--config", str(tmp_cfg_path)])
    result = cli_runner.invoke(app, ["init", "-t", "vsm_b", "--config", str(tmp_cfg_path)])
    assert result.exit_code == 1
    assert "already exists" in result.output


def test_cli_init_force_overwrites(cli_runner: CliRunner, tmp_cfg_path: Path) -> None:
    from employer_voice import config as cfgmod
    from employer_voice.cli import app

    cli_runner.invoke(app, ["init", "-t", "vsm_a", "--config", str(tmp_cfg_path)])
    result = cli_runner.invoke(app, ["init", "-t", "vsm_b", "--force", "--config", str(tmp_cfg_path)])
    assert result.exit_code == 0
    assert cfgmod.load(tmp_cfg_path).replica.manager_token == "vsm_b"


def test_cli_config_masks_token(cli_runner: CliRunner, tmp_cfg_path: Path) -> None:
    from employer_voice.cli import app

    cli_runner.invoke(app, ["init", "-t", "vsm_supersecret_token_abcdef", "--config", str(tmp_cfg_path)])
    result = cli_runner.invoke(app, ["config", "--config", str(tmp_cfg_path)])
    assert result.exit_code == 0
    assert "vsm_supersecret_token_abcdef" not in result.output
    assert "vsm_su" in result.output
    assert "cdef" in result.output


def test_cli_ask_prints_assistant_message(cli_runner: CliRunner, tmp_cfg_path: Path) -> None:
    from employer_voice.cli import app
    from employer_voice.replica_client import ChatResult

    cli_runner.invoke(app, ["init", "-t", "vsm_t", "--config", str(tmp_cfg_path)])

    fake = ChatResult(
        conversation_id="conv-1",
        assistant_message="Alice is working on auth.",
        tool_calls=[{"tool_use_id": "x", "tool_name": "get_timeline"}],
        stop_reason="end_turn",
        loops=2,
        model="claude-sonnet-4-6",
        budget_exhausted=False,
    )

    async def _fake_chat(self, message, conversation_id=None, *, client=None):
        return fake

    with patch("employer_voice.replica_client.ReplicaClient.chat", _fake_chat):
        result = cli_runner.invoke(app, [
            "ask", "What's Alice doing?", "--config", str(tmp_cfg_path),
        ])
    assert result.exit_code == 0, result.output
    assert "Alice is working on auth." in result.output
    assert "1 tool call" in result.output


def test_cli_ask_handles_errors_nonzero(cli_runner: CliRunner, tmp_cfg_path: Path) -> None:
    from employer_voice.cli import app

    # No config → missing token → non-zero exit
    result = cli_runner.invoke(app, [
        "ask", "hi", "--config", str(tmp_cfg_path),
    ])
    assert result.exit_code == 1
    assert "Error" in result.output


def test_cli_conversations_lists(cli_runner: CliRunner, tmp_cfg_path: Path) -> None:
    from employer_voice.cli import app

    cli_runner.invoke(app, ["init", "-t", "vsm_t", "--config", str(tmp_cfg_path)])

    async def _fake_list(self, limit=20, *, client=None):
        return [
            {"conversation_id": "aaaaaaaa-1111", "title": "First chat", "turn_count": 4, "updated_at": "2026-10-26T00:00:00Z"},
            {"conversation_id": "bbbbbbbb-2222", "title": "Second chat", "turn_count": 1, "updated_at": "2026-10-26T01:00:00Z"},
        ]

    with patch("employer_voice.replica_client.ReplicaClient.list_conversations", _fake_list):
        result = cli_runner.invoke(app, ["conversations", "--config", str(tmp_cfg_path)])
    assert result.exit_code == 0, result.output
    assert "First chat" in result.output
    assert "Second chat" in result.output
    assert "aaaaaaaa" in result.output


def test_cli_reset_clears_conversation_id(cli_runner: CliRunner, tmp_cfg_path: Path) -> None:
    from employer_voice import config as cfgmod
    from employer_voice.cli import app

    cli_runner.invoke(app, ["init", "-t", "vsm_t", "--config", str(tmp_cfg_path)])
    cfgmod.update_conversation_id("conv-abc", tmp_cfg_path)
    assert cfgmod.load(tmp_cfg_path).conversation_id == "conv-abc"

    result = cli_runner.invoke(app, ["reset", "--config", str(tmp_cfg_path)])
    assert result.exit_code == 0
    assert "Cleared" in result.output
    assert cfgmod.load(tmp_cfg_path).conversation_id == ""


# ── Smoke: package has no voice/talk surface ────────────────────────────────


def test_voice_subpackage_is_gone() -> None:
    with pytest.raises(ModuleNotFoundError):
        import employer_voice.voice  # noqa: F401


def test_cli_has_no_talk_command(cli_runner: CliRunner) -> None:
    from employer_voice.cli import app

    result = cli_runner.invoke(app, ["talk"])
    # Typer prints "No such command 'talk'" and exits non-zero.
    assert result.exit_code != 0


def test_package_has_main_entrypoint() -> None:
    import employer_voice

    assert callable(employer_voice.main)
    assert employer_voice.app is not None
