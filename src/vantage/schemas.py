from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Generic, TypeVar
from uuid import uuid4

from pydantic import BaseModel, Field

T = TypeVar("T")


class Channel(str, Enum):
    WEEKLY_DIGEST = "weekly_digest"
    COMMIT_REPORT = "commit_report"
    STANDUP = "standup"
    PR_DRAFT = "pr_draft"
    LIVE_PROBE = "live_probe"
    QUICK_NOTE = "quick_note"


class RedactionLevel(str, Enum):
    MINIMAL = "minimal"
    STANDARD = "standard"
    STRICT = "strict"


class PolicyDecision(str, Enum):
    ALLOWED = "allowed"
    DENIED_BY_POLICY = "denied_by_policy"
    OFFLINE = "offline"


class ApprovalSource(str, Enum):
    MANUAL = "manual"
    POLICY = "policy"
    NONE = "none"


class Turn(BaseModel):
    role: str
    content_excerpt: str
    tool_name: str | None = None
    timestamp: datetime | None = None


class ActivitySignals(BaseModel):
    files_recently_touched: list[str] = Field(default_factory=list)
    inferred_branch: str = ""
    active_repo: str = ""
    last_user_intent: str = ""
    primary_activity: str = "unknown"  # editing | debugging | reviewing | browsing | unknown
    session_active_since: datetime | None = None


class SessionSnapshot(BaseModel):
    developer_id: str
    agent_kind: str
    session_id: str
    cwd: str
    last_turns: list[Turn]
    activity_signals: ActivitySignals | None = None
    captured_at: datetime = Field(default_factory=datetime.utcnow)


class Redaction(BaseModel):
    kind: str
    placeholder: str
    position: int


class RedactedContext(BaseModel):
    original_length: int
    scrubbed_text: str
    redactions: list[Redaction]
    file_path: str | None = None


class WeeklyDigest(BaseModel):
    period_start: datetime
    period_end: datetime
    highlights: list[str]
    commits: list[str]
    open_threads: list[str]
    blockers: list[str]
    next_week: list[str]
    redactions_summary: str


class CommitReport(BaseModel):
    report_id: str = Field(default_factory=lambda: str(uuid4()))
    developer_id: str
    repo: str
    commit_sha: str
    branch: str
    diff_stat: str
    files_touched: list[str]
    author_msg: str
    generated_summary: str
    open_questions: list[str] = Field(default_factory=list)
    confidence: float = 0.8
    created_at: datetime = Field(default_factory=datetime.utcnow)


class IncrementalSummary(BaseModel):
    summary_id: str = Field(default_factory=lambda: str(uuid4()))
    developer_id: str
    scope: str
    source_refs: list[str]
    text: str
    tags: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class StandupBullets(BaseModel):
    yesterday: list[str]
    today: list[str]
    blockers: list[str] = Field(default_factory=list)


class PRDraft(BaseModel):
    branch: str
    base: str
    title: str
    summary: str
    rationale: str
    test_notes: str
    risk_notes: str


class QuickNote(BaseModel):
    message: str
    context: str = ""  # optional freeform context (branch, repo, etc.)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class DraftPayload(BaseModel, Generic[T]):
    draft_id: str = Field(default_factory=lambda: str(uuid4()))
    artifact_type: str
    payload: T
    created_at: datetime = Field(default_factory=datetime.utcnow)
    developer_id: str

    model_config = {"arbitrary_types_allowed": True}


class ApprovedPayload(BaseModel, Generic[T]):
    """Construct only via ApprovalGate — enforced by convention and type checking."""
    approval_id: str = Field(default_factory=lambda: str(uuid4()))
    draft_id: str
    artifact_type: str
    payload: T
    approved_at: datetime = Field(default_factory=datetime.utcnow)
    approval_source: ApprovalSource
    developer_id: str
    redaction_level: RedactionLevel = RedactionLevel.STANDARD

    model_config = {"arbitrary_types_allowed": True}


class SubscriberIdentity(BaseModel):
    subscriber_id: str = Field(default_factory=lambda: f"sub_{uuid4().hex[:12]}")
    name: str
    receive_url: str
    channels: list[Channel] = Field(default_factory=list)
    token_value: str = ""
    revocation_counter: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)
    revoked_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None


class SharingPolicy(BaseModel):
    subscriber_id: str
    channels: list[Channel] = Field(default_factory=list)
    redaction_level: RedactionLevel = RedactionLevel.STANDARD
    auto_approve: bool = False
    # Live-probe gating (M4)
    probe_hours_start: int | None = None   # 0–23 UTC; None = no restriction
    probe_hours_end: int | None = None     # 0–23 UTC exclusive; None = no restriction
    probe_repos_allow: list[str] = Field(default_factory=list)  # empty = allow all
    probe_repos_deny: list[str] = Field(default_factory=list)


class ScopedTokenClaims(BaseModel):
    token_id: str = Field(default_factory=lambda: str(uuid4()))
    subscriber_id: str
    developer_id: str
    channels: list[Channel]
    issued_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: datetime
    revocation_counter: int = 0


class ProbeRequest(BaseModel):
    request_id: str = Field(default_factory=lambda: str(uuid4()))
    subscriber_id: str
    repo_hint: str = ""
    requested_at: datetime = Field(default_factory=datetime.utcnow)


class ProbeResponse(BaseModel):
    request_id: str
    developer_id: str
    policy_decision: PolicyDecision
    snapshot: SessionSnapshot | None = None
    fallback_summary: str = ""
    staleness_seconds: int = 0
    redaction_level_applied: RedactionLevel = RedactionLevel.STANDARD
    responded_at: datetime = Field(default_factory=datetime.utcnow)


class AuditEntry(BaseModel):
    entry_id: str = Field(default_factory=lambda: str(uuid4()))
    ts: datetime = Field(default_factory=datetime.utcnow)
    kind: str
    developer_id: str
    destination: str | None = None
    pre_redaction_sha256: str
    redactions: list[Redaction]
    scrubbed_input_excerpt: str
    model: str
    output: str
    approval_source: ApprovalSource = ApprovalSource.NONE
