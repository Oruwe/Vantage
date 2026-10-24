from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shadow_replica.notify.notifier import (
    NotificationConfig,
    _artifact_summary,
    dispatch_all,
)


# ── _artifact_summary ─────────────────────────────────────────────────────────

def _payload(**kwargs) -> str:
    return json.dumps(kwargs)


def test_summary_weekly_digest():
    subject, body = _artifact_summary(
        "weekly_digest",
        "Alice",
        _payload(highlights=["shipped auth", "fixed bug"]),
    )
    assert "Weekly digest" in subject
    assert "Alice" in subject
    assert "shipped auth" in body


def test_summary_commit_report():
    subject, body = _artifact_summary(
        "commit_report",
        "Bob",
        _payload(branch="feature/x", author_msg="add login", generated_summary="Adds login form"),
    )
    assert "Commit report" in subject
    assert "feature/x" in body


def test_summary_standup():
    subject, body = _artifact_summary(
        "standup",
        "Carol",
        _payload(yesterday=["fixed tests"], today=["write docs"]),
    )
    assert "Standup" in subject
    assert "fixed tests" in body
    assert "write docs" in body


def test_summary_pr_draft():
    subject, body = _artifact_summary(
        "pr_draft",
        "Dave",
        _payload(title="Add dark mode", summary="Switches theme"),
    )
    assert "PR draft" in subject
    assert "Add dark mode" in body


def test_summary_unknown_type():
    subject, body = _artifact_summary("custom_thing", "Eve", "{}")
    assert "Eve" in subject
    assert "custom thing" in body.lower()


# ── dispatch_all ──────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_dispatch_no_configs():
    # Should be a no-op with no configs
    await dispatch_all([], "weekly_digest", "Alice", "dev-1", "{}")


@pytest.mark.anyio
async def test_dispatch_slack_webhook():
    cfg = NotificationConfig(type="slack_webhook", url="https://hooks.slack.com/fake")
    mock_resp = MagicMock()
    mock_resp.is_success = True

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        await dispatch_all(
            [cfg],
            "commit_report",
            "Alice",
            "dev-1",
            _payload(branch="main", author_msg="add feature"),
        )

    mock_client.post.assert_awaited_once()
    call_kwargs = mock_client.post.call_args
    assert call_kwargs[0][0] == "https://hooks.slack.com/fake"
    payload = call_kwargs[1]["json"]
    assert "text" in payload
    assert "Alice" in payload["text"]


@pytest.mark.anyio
async def test_dispatch_generic_webhook():
    cfg = NotificationConfig(type="generic_webhook", url="https://example.com/hook")
    mock_resp = MagicMock()
    mock_resp.is_success = True

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        await dispatch_all(
            [cfg],
            "pr_draft",
            "Bob",
            "dev-2",
            _payload(title="My PR"),
        )

    call_kwargs = mock_client.post.call_args
    body = call_kwargs[1]["json"]
    assert body["artifact_type"] == "pr_draft"
    assert body["developer_id"] == "dev-2"
    assert body["developer_name"] == "Bob"
    assert body["payload"]["title"] == "My PR"


@pytest.mark.anyio
async def test_dispatch_artifact_type_filter_blocks():
    cfg = NotificationConfig(
        type="slack_webhook",
        url="https://hooks.slack.com/fake",
        artifact_types=["weekly_digest"],  # only weekly_digest
    )
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value = mock_client

        await dispatch_all(
            [cfg],
            "commit_report",  # should be filtered out
            "Alice",
            "dev-1",
            "{}",
        )

    mock_client.post.assert_not_called()


@pytest.mark.anyio
async def test_dispatch_artifact_type_filter_allows():
    cfg = NotificationConfig(
        type="slack_webhook",
        url="https://hooks.slack.com/fake",
        artifact_types=["commit_report"],
    )
    mock_resp = MagicMock()
    mock_resp.is_success = True

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        await dispatch_all(
            [cfg],
            "commit_report",
            "Alice",
            "dev-1",
            _payload(branch="main", author_msg="fix"),
        )

    mock_client.post.assert_awaited_once()


@pytest.mark.anyio
async def test_dispatch_slack_failure_does_not_raise():
    cfg = NotificationConfig(type="slack_webhook", url="https://hooks.slack.com/fake")
    mock_resp = MagicMock()
    mock_resp.is_success = False
    mock_resp.status_code = 500
    mock_resp.text = "Internal Server Error"

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        # Should not raise
        await dispatch_all([cfg], "weekly_digest", "Alice", "dev-1", "{}")


@pytest.mark.anyio
async def test_dispatch_unknown_type_skipped():
    cfg = NotificationConfig(type="pigeon_mail", url="http://nowhere")
    await dispatch_all([cfg], "weekly_digest", "Alice", "dev-1", "{}")


# ── Config parsing roundtrip ───────────────────────────────────────────────────

def test_notification_config_defaults():
    cfg = NotificationConfig(type="slack_webhook")
    assert cfg.url == ""
    assert cfg.artifact_types == []
    assert cfg.port == 587


def test_notification_config_smtp_fields():
    cfg = NotificationConfig(
        type="smtp",
        host="smtp.example.com",
        port=465,
        username="bot@example.com",
        password_env="SMTP_PASS",
        from_addr="bot@example.com",
        to_addrs=["manager@company.com"],
        artifact_types=["weekly_digest"],
    )
    assert cfg.host == "smtp.example.com"
    assert cfg.to_addrs == ["manager@company.com"]
