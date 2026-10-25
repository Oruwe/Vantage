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


class CounterOffer(BaseModel):
    """A downgrade the dev's policy agent is willing to share instead."""
    approval_id: str = ""        # pointer to a previously-approved artifact (if available)
    artifact_type: str = ""      # "commit_report" | "standup" | …
    summary: str = ""            # human-readable one-liner
    created_at: datetime | None = None  # when the underlying artifact was approved
    reason: str = ""             # why the live probe was denied


class ProbeResponse(BaseModel):
    request_id: str
    developer_id: str
    policy_decision: PolicyDecision
    snapshot: SessionSnapshot | None = None
    fallback_summary: str = ""
    staleness_seconds: int = 0
    redaction_level_applied: RedactionLevel = RedactionLevel.STANDARD
    responded_at: datetime = Field(default_factory=datetime.utcnow)
    counter_offer: CounterOffer | None = None


class IntentBeacon(BaseModel):
    """A ghost's periodic broadcast of what it's actively working on.

    Lightweight by design — files + repo + a scrubbed one-liner — so the replica
    can detect semantic collisions without the ghost ever shipping raw session
    content.
    """
    beacon_id: str = Field(default_factory=lambda: str(uuid4()))
    developer_id: str
    repo: str = ""
    branch: str = ""
    files_touched: list[str] = Field(default_factory=list)
    intent_hint: str = ""   # scrubbed one-liner summary of current work
    turn_count: int = 0     # how many turns in the active session
    emitted_at: datetime = Field(default_factory=datetime.utcnow)


class CollisionCandidate(BaseModel):
    """A pair of ghosts the replica has detected as overlapping."""
    repo: str
    developer_a: str
    developer_b: str
    overlapping_files: list[str]
    intent_a: str = ""
    intent_b: str = ""
    jaccard: float = 0.0
    detected_at: datetime = Field(default_factory=datetime.utcnow)


class FrictionReport(BaseModel):
    """Rolling AI-friction snapshot for one developer.

    `score` is 0.0 (frictionless) → 1.0 (deeply stuck). Derived locally from
    tool-call failures, repeated error patterns, token burn, and the gap
    between the last commit and the latest session activity.
    """
    report_id: str = Field(default_factory=lambda: str(uuid4()))
    developer_id: str
    score: float = 0.0
    turns_scanned: int = 0
    failed_tool_calls: int = 0
    repeated_errors: int = 0
    minutes_since_commit: int = 0
    hot_files: list[str] = Field(default_factory=list)
    summary: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class InitiativeEvent(BaseModel):
    """One micro-event inside an initiative's timeline."""
    kind: str              # "shipped" | "in_flight" | "blocker"
    text: str
    developer_id: str
    developer_name: str = ""
    artifact_type: str
    approval_id: str = ""
    at: datetime | None = None


class Initiative(BaseModel):
    """A clustered theme of recent work across the team.

    Produced by the clusterer from recent commits, standups, and digests.
    Each initiative groups artifacts that share semantic / lexical overlap so
    the manager sees team-level narratives instead of a per-developer wall.
    """
    initiative_id: str                      # stable hash of label (slug)
    label: str                              # 2–4 word human-readable name
    period_start: datetime
    period_end: datetime
    contributors: list[str] = Field(default_factory=list)       # developer_ids
    contributor_names: list[str] = Field(default_factory=list)
    artifact_count: int = 0
    commit_count: int = 0
    shipped: list[str] = Field(default_factory=list)
    in_flight: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    top_terms: list[str] = Field(default_factory=list)
    timeline: list[InitiativeEvent] = Field(default_factory=list)
    momentum_score: float = 0.0             # 0 → cold, 1 → hot (recent + many contribs)
    risk_flag: bool = False                 # true if blockers present or momentum dropping


class BugResolution(BaseModel):
    """A struggle-then-success pattern captured from a dev's session.

    Produced by the Deja-Vu detector when it sees multiple failed tool calls
    with similar error signatures followed by a successful fix. Approved
    resolutions are pushed to shadow-replica and indexed so another developer
    hitting the same error can retrieve the winning fix instantly.
    """
    resolution_id: str = Field(default_factory=lambda: str(uuid4()))
    developer_id: str
    error_signature: str      # normalized — paths/line-numbers/PIDs stripped
    error_sample: str = ""    # short raw sample of one failing invocation (scrubbed)
    attempt_count: int = 0    # how many failing turns preceded the fix
    fix_summary: str = ""     # one-line human-readable fix description
    fix_details: str = ""     # scrubbed excerpt of the resolving turn(s)
    files_touched: list[str] = Field(default_factory=list)
    tools_used: list[str] = Field(default_factory=list)
    repo: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class SemanticCollision(BaseModel):
    """Two devs working on semantically similar things, detected via embeddings.

    Complementary to `CollisionCandidate` (file-overlap Jaccard on live beacons):
    this fires on artifacts that share meaning even when the files don't overlap,
    e.g. "Alice refactoring auth middleware" + "Bob rewriting session validation"
    in different files but the same concept.
    """
    collision_id: str = Field(default_factory=lambda: str(uuid4()))
    developer_a: str
    developer_b: str
    developer_a_name: str = ""
    developer_b_name: str = ""
    artifact_a_id: str
    artifact_b_id: str
    artifact_a_type: str
    artifact_b_type: str
    artifact_a_summary: str = ""
    artifact_b_summary: str = ""
    cosine_similarity: float = 0.0
    shared_terms: list[str] = Field(default_factory=list)   # TF-IDF-ish token overlap
    file_overlap: list[str] = Field(default_factory=list)   # files that appear in both
    severity: str = "medium"                                # "high" | "medium" | "low"
    detected_at: datetime = Field(default_factory=datetime.utcnow)


class GithubEvent(BaseModel):
    """A noteworthy event from a watched GitHub repo, received via webhook.

    Attributed to a developer when their `github_login` matches `author_login`;
    otherwise filed under `developer_id="unmapped:<login>"` so nothing is lost
    and later pairing can reclaim it retroactively.
    """
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    event_type: str                            # "pull_request.opened" | "pull_request_review.submitted" | …
    action: str = ""                           # the raw GitHub action, e.g. "opened"
    repo: str                                  # "owner/name"
    pr_number: int | None = None
    title: str = ""
    url: str = ""                              # html_url for the PR / review / comment
    author_login: str = ""                     # GitHub login of the actor
    branch: str = ""
    commit_sha: str = ""
    summary: str = ""                          # deterministic one-liner, e.g. "Alice opened PR #42: …"
    raw_payload_excerpt: str = ""              # cleaned engineering text, ≤2KB (NOT raw JSON)
    received_at: datetime = Field(default_factory=datetime.utcnow)
    # Filled in by the reactive-followup task when a trigger event lands (CI
    # failure, review changes-requested). Describes the dev's live session
    # state at the moment the event was received — "Alice is actively editing
    # test_payments.py (3 turns ago)."
    followup_summary: str = ""
    followup_status: str = ""                  # "online_cached" | "online_probed" | "offline" | "unmapped" | "no_trigger"


class DailyBrief(BaseModel):
    """Replica-side synthesis of yesterday across the whole team."""
    brief_id: str = Field(default_factory=lambda: str(uuid4()))
    period_start: datetime
    period_end: datetime
    shipped: list[str] = Field(default_factory=list)
    in_flight: list[str] = Field(default_factory=list)
    bottlenecks: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    collisions: list[SemanticCollision] = Field(default_factory=list)
    headline: str = ""
    developer_count: int = 0
    artifact_count: int = 0
    generated_at: datetime = Field(default_factory=datetime.utcnow)


class ChatTurn(BaseModel):
    """One turn in a manager-agent conversation.

    Role is one of "user" | "assistant" | "tool". For tool-result turns, both
    `tool_name` and `tool_call_id` are set and `content` holds the JSON-encoded
    tool output. The Anthropic SDK's content-block structure is reconstructed
    from the stored primitives on each replay.
    """
    turn_id: str = Field(default_factory=lambda: str(uuid4()))
    conversation_id: str
    role: str
    content: str
    tool_name: str = ""
    tool_call_id: str = ""
    tool_input_json: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ChatConversation(BaseModel):
    """A thread of ChatTurns owned by exactly one manager."""
    conversation_id: str = Field(default_factory=lambda: str(uuid4()))
    manager_token_hash: str
    title: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_turn_at: datetime = Field(default_factory=datetime.utcnow)
    turn_count: int = 0


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
