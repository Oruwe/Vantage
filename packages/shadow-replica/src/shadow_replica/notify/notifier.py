from __future__ import annotations

import json
import logging
import os
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


@dataclass
class NotificationConfig:
    type: str                          # slack_webhook | smtp | generic_webhook
    url: str = ""                      # used by slack_webhook and generic_webhook
    # SMTP-only fields
    host: str = ""
    port: int = 587
    username: str = ""
    password_env: str = ""
    from_addr: str = ""
    to_addrs: list[str] = field(default_factory=list)
    # Filter: empty list = notify for all artifact types
    artifact_types: list[str] = field(default_factory=list)


def _artifact_summary(artifact_type: str, developer_name: str, payload_json: str) -> tuple[str, str]:
    """Return (subject, body) for the notification."""
    try:
        payload = json.loads(payload_json)
    except Exception:
        payload = {}

    subject_map = {
        "weekly_digest": "Weekly digest",
        "commit_report": "Commit report",
        "standup": "Standup update",
        "pr_draft": "PR draft",
        "quick_note": "Note",
    }
    subject_base = subject_map.get(artifact_type, artifact_type.replace("_", " ").title())
    subject = f"[Vantage] {subject_base} from {developer_name}"

    if artifact_type == "weekly_digest":
        highlights = payload.get("highlights", [])
        body = f"{developer_name} sent a weekly digest.\n\nHighlights:\n"
        body += "\n".join(f"• {h}" for h in highlights[:5]) if highlights else "(no highlights)"
    elif artifact_type == "commit_report":
        branch = payload.get("branch", "")
        msg = payload.get("author_msg", "")
        summary = payload.get("generated_summary", "")
        body = f"{developer_name} pushed a commit on {branch!r}.\n\n{msg}\n\n{summary}"
    elif artifact_type == "standup":
        yesterday = payload.get("yesterday", [])
        today = payload.get("today", [])
        body = f"{developer_name}'s standup:\n\nYesterday:\n"
        body += "\n".join(f"• {b}" for b in yesterday) or "(none)"
        body += "\n\nToday:\n"
        body += "\n".join(f"• {b}" for b in today) or "(none)"
    elif artifact_type == "pr_draft":
        title = payload.get("title", "(untitled)")
        summary = payload.get("summary", "")
        body = f"{developer_name} shared a PR draft: {title}\n\n{summary}"
    elif artifact_type == "quick_note":
        message = payload.get("message", "")
        context = payload.get("context", "")
        body = f"{developer_name}: {message}"
        if context:
            body += f"\n\nContext: {context}"
    else:
        body = f"{developer_name} sent a {artifact_type.replace('_', ' ')}."

    return subject, body.strip()


async def dispatch_all(
    configs: list[NotificationConfig],
    artifact_type: str,
    developer_name: str,
    developer_id: str,
    payload_json: str,
) -> None:
    """Fire all matching notification configs asynchronously. Never raises."""
    if not configs:
        return

    subject, body = _artifact_summary(artifact_type, developer_name, payload_json)

    for cfg in configs:
        if cfg.artifact_types and artifact_type not in cfg.artifact_types:
            continue
        try:
            if cfg.type == "slack_webhook":
                await _send_slack(cfg, subject, body)
            elif cfg.type == "smtp":
                _send_smtp(cfg, subject, body)
            elif cfg.type == "generic_webhook":
                await _send_generic(cfg, artifact_type, developer_id, developer_name, payload_json)
            else:
                logger.warning("Unknown notification type %r — skipping", cfg.type)
        except Exception as exc:
            logger.warning("Notification dispatch failed (%s): %s", cfg.type, exc)


async def _send_slack(cfg: NotificationConfig, subject: str, body: str) -> None:
    if not cfg.url:
        logger.warning("slack_webhook: no url configured")
        return
    text = f"*{subject}*\n{body}"
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.post(cfg.url, json={"text": text})
    if not resp.is_success:
        logger.warning("Slack webhook returned %d: %s", resp.status_code, resp.text[:200])
    else:
        logger.info("Slack notification sent (%s)", subject)


async def _send_generic(
    cfg: NotificationConfig,
    artifact_type: str,
    developer_id: str,
    developer_name: str,
    payload_json: str,
) -> None:
    if not cfg.url:
        logger.warning("generic_webhook: no url configured")
        return
    try:
        payload = json.loads(payload_json)
    except Exception:
        payload = {}
    data = {
        "artifact_type": artifact_type,
        "developer_id": developer_id,
        "developer_name": developer_name,
        "payload": payload,
    }
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.post(cfg.url, json=data)
    if not resp.is_success:
        logger.warning("Generic webhook returned %d: %s", resp.status_code, resp.text[:200])
    else:
        logger.info("Generic webhook notification sent (%s)", artifact_type)


def _send_smtp(cfg: NotificationConfig, subject: str, body: str) -> None:
    if not cfg.host or not cfg.to_addrs:
        logger.warning("smtp: host or to_addrs not configured")
        return
    password = os.environ.get(cfg.password_env, "") if cfg.password_env else ""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.from_addr or cfg.username
    msg["To"] = ", ".join(cfg.to_addrs)
    msg.set_content(body)

    context = ssl.create_default_context()
    try:
        with smtplib.SMTP(cfg.host, cfg.port, timeout=10) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            if cfg.username and password:
                smtp.login(cfg.username, password)
            smtp.send_message(msg)
        logger.info("SMTP notification sent to %s", cfg.to_addrs)
    except Exception as exc:
        raise RuntimeError(f"SMTP send failed: {exc}") from exc
