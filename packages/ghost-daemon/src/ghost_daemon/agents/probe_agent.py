from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from vantage.schemas import (
    Channel, CounterOffer, PolicyDecision, ProbeRequest, ProbeResponse,
    RedactionLevel, SessionSnapshot, Turn,
)

_SYSTEM_SUBSCRIBER = "replica:system"
from ghost_daemon.agents.activity_extractor import extract_activity_signals
from ghost_daemon.approval.policy import load_policy
from ghost_daemon.config import Config
from ghost_daemon.models.registry import LLMProvider
from ghost_daemon.redaction.scrubber import scrub
from ghost_daemon.watchers.session_tail import SessionTailer

if TYPE_CHECKING:
    from ghost_daemon.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


class ProbeAgent:
    """Handles incoming live-probe requests from the dev's ws_client.

    Policy is evaluated on the dev's machine — the subscriber never bypasses it.
    When policy denies the live tail, the agent builds a `CounterOffer`
    pointing at the most-recent *already-approved* commit so the manager isn't
    stonewalled — a visible, auditable downgrade instead of a flat refusal.
    """

    def __init__(
        self,
        cfg: Config,
        llm: LLMProvider,
        tailer: SessionTailer,
        store: "SQLiteStore | None" = None,
    ) -> None:
        self._cfg = cfg
        self._llm = llm
        self._tailer = tailer
        self._store = store

    async def run(self, request: ProbeRequest) -> ProbeResponse:
        # Reactive follow-up probes from the replica (triggered by a CI failure
        # or a changes-requested review on *this* dev's work) bypass the per-
        # subscriber policy and return activity signals only — never raw turns.
        # The question is "is Alice already looking at this?", not "show me her
        # code" — so the strict path is both safer AND more useful.
        if request.subscriber_id == _SYSTEM_SUBSCRIBER:
            snapshot = await self._collect_snapshot()
            snapshot.last_turns = []  # strict: never leak content for reactive probes
            logger.info(
                "Reactive-followup probe answered (activity-only) for repo_hint=%r",
                request.repo_hint,
            )
            return ProbeResponse(
                request_id=request.request_id,
                developer_id=self._cfg.developer_id,
                policy_decision=PolicyDecision.ALLOWED,
                snapshot=snapshot,
                redaction_level_applied=RedactionLevel.STRICT,
            )

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
            counter = self._build_counter_offer(decision, policy)
            return ProbeResponse(
                request_id=request.request_id,
                developer_id=self._cfg.developer_id,
                policy_decision=decision,
                fallback_summary=(
                    counter.summary if counter and counter.summary
                    else "Response not available due to policy."
                ),
                redaction_level_applied=policy.redaction_level,
                counter_offer=counter,
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

    # ── Counter-offer construction ────────────────────────────────────────────

    def _build_counter_offer(self, decision: PolicyDecision, policy) -> CounterOffer | None:
        """Offer a bounded downgrade: pointer to the most recent approved commit.

        No new data crosses the wire — the shadow-replica already has this
        artifact. The counter-offer is a *handle* the manager UI can resolve
        locally while still respecting that the live tail is withheld.
        """
        reason = self._deny_reason(decision, policy)
        store = self._store
        if store is None:
            return CounterOffer(reason=reason)

        try:
            rows = store.list_recent(limit=50)
        except Exception as exc:
            logger.debug("Counter-offer lookup failed: %s", exc)
            return CounterOffer(reason=reason)

        for row in rows:
            if row.status != "approved":
                continue
            if row.artifact_type not in ("commit_report", "standup"):
                continue
            summary = self._row_summary(row)
            if not summary:
                continue
            created = None
            if row.created_at:
                try:
                    created = datetime.fromisoformat(row.created_at)
                except Exception:
                    created = None
            return CounterOffer(
                approval_id=getattr(row, "approval_id", "") or getattr(row, "draft_id", ""),
                artifact_type=row.artifact_type,
                summary=f"Live tail withheld. Last approved {row.artifact_type}: {summary}",
                created_at=created,
                reason=reason,
            )
        return CounterOffer(reason=reason)

    @staticmethod
    def _deny_reason(decision: PolicyDecision, policy) -> str:
        if decision == PolicyDecision.OFFLINE:
            return "offline"
        parts = []
        if policy.probe_hours_start is not None and policy.probe_hours_end is not None:
            parts.append(f"hours {policy.probe_hours_start:02d}-{policy.probe_hours_end:02d} UTC")
        if policy.probe_repos_deny:
            parts.append("repo denylist")
        elif policy.probe_repos_allow:
            parts.append("repo allowlist")
        if not parts:
            return "channel not enabled"
        return "policy: " + ", ".join(parts)

    @staticmethod
    def _row_summary(row) -> str:
        src = row.edited_payload_json or getattr(row, "payload_json", None)
        if not src:
            return ""
        try:
            payload = json.loads(src)
        except Exception:
            return ""
        if row.artifact_type == "commit_report":
            return (payload.get("author_msg") or payload.get("generated_summary") or "")[:140]
        if row.artifact_type == "standup":
            today = payload.get("today", [])
            return today[0][:140] if today else ""
        return ""

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
