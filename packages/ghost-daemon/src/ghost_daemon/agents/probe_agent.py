from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from vantage.schemas import (
    Channel, PolicyDecision, ProbeRequest, ProbeResponse, RedactionLevel,
    SessionSnapshot, Turn,
)
from ghost_daemon.agents.activity_extractor import extract_activity_signals
from ghost_daemon.approval.policy import load_policy
from ghost_daemon.config import Config
from ghost_daemon.models.registry import LLMProvider
from ghost_daemon.redaction.scrubber import scrub
from ghost_daemon.watchers.session_tail import SessionTailer

logger = logging.getLogger(__name__)


class ProbeAgent:
    """Handles incoming live-probe requests from the dev's ws_client.

    Policy is evaluated on the dev's machine — the subscriber never bypasses it.
    """

    def __init__(self, cfg: Config, llm: LLMProvider, tailer: SessionTailer) -> None:
        self._cfg = cfg
        self._llm = llm
        self._tailer = tailer

    async def run(self, request: ProbeRequest) -> ProbeResponse:
        policy = load_policy(request.subscriber_id, self._cfg.vantage_dir)

        decision = self._check_policy(policy, request)
        if decision != PolicyDecision.ALLOWED:
            logger.info(
                "Probe from subscriber %s denied (%s): hours=%s-%s repo_hint=%r",
                request.subscriber_id,
                decision.value,
                policy.probe_hours_start,
                policy.probe_hours_end,
                request.repo_hint,
            )
            return ProbeResponse(
                request_id=request.request_id,
                developer_id=self._cfg.developer_id,
                policy_decision=decision,
                fallback_summary="Response not available due to policy.",
                redaction_level_applied=policy.redaction_level,
            )

        snapshot = await self._collect_snapshot()
        logger.info(
            "Probe from subscriber %s answered with %d turns",
            request.subscriber_id,
            len(snapshot.last_turns),
        )
        return ProbeResponse(
            request_id=request.request_id,
            developer_id=self._cfg.developer_id,
            policy_decision=PolicyDecision.ALLOWED,
            snapshot=snapshot,
            redaction_level_applied=policy.redaction_level,
        )

    # ── Policy gating ─────────────────────────────────────────────────────────

    def _check_policy(self, policy, request: ProbeRequest) -> PolicyDecision:
        if Channel.LIVE_PROBE not in policy.channels:
            return PolicyDecision.DENIED_BY_POLICY

        now_hour = datetime.utcnow().hour
        h_start = policy.probe_hours_start
        h_end = policy.probe_hours_end
        if h_start is not None and h_end is not None:
            if h_start <= h_end:
                allowed = h_start <= now_hour < h_end
            else:
                # wraps midnight: e.g. 22–6
                allowed = now_hour >= h_start or now_hour < h_end
            if not allowed:
                return PolicyDecision.DENIED_BY_POLICY

        hint = request.repo_hint.lower()
        if policy.probe_repos_allow and hint:
            if not any(r.lower() in hint for r in policy.probe_repos_allow):
                return PolicyDecision.DENIED_BY_POLICY

        if policy.probe_repos_deny and hint:
            if any(r.lower() in hint for r in policy.probe_repos_deny):
                return PolicyDecision.DENIED_BY_POLICY

        return PolicyDecision.ALLOWED

    # ── Session snapshot ──────────────────────────────────────────────────────

    async def _collect_snapshot(self) -> SessionSnapshot:
        raw_turns = []
        for session_dir in self._cfg.resolved_session_dirs:
            jsonl_files = sorted(
                session_dir.rglob("*.jsonl"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for jsonl in jsonl_files[:5]:
                try:
                    turns = await self._tailer.tail_recent(jsonl, n=15)
                    raw_turns.extend(turns)
                    if len(raw_turns) >= 30:
                        break
                except Exception:
                    continue
            if len(raw_turns) >= 30:
                break

        scrubbed: list[Turn] = []
        for t in raw_turns[:30]:
            rc = scrub(f"[{t.role}] {t.content_excerpt}")
            scrubbed.append(Turn(
                role=t.role,
                content_excerpt=rc.scrubbed_text[:600],
                tool_name=t.tool_name,
                timestamp=t.timestamp,
            ))

        cwd = str(Path.cwd())
        signals = extract_activity_signals(scrubbed, cwd=cwd)
        return SessionSnapshot(
            developer_id=self._cfg.developer_id,
            agent_kind="claude_code",
            session_id=str(uuid4()),
            cwd=cwd,
            last_turns=scrubbed,
            activity_signals=signals,
        )
