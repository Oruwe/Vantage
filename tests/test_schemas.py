from datetime import datetime

from vantage.schemas import (
    ApprovalSource,
    ApprovedPayload,
    Channel,
    DraftPayload,
    PRDraft,
    RedactionLevel,
    SharingPolicy,
    StandupBullets,
    SubscriberIdentity,
    WeeklyDigest,
)

_NOW = datetime(2026, 10, 24, 12, 0, 0)


def _digest() -> WeeklyDigest:
    return WeeklyDigest(
        period_start=datetime(2026, 10, 18),
        period_end=_NOW,
        highlights=["Shipped M5"],
        commits=["abc123"],
        open_threads=[],
        blockers=[],
        next_week=["M6 CI/CD"],
        redactions_summary="0 redactions",
    )


def test_channel_enum_values():
    assert Channel.WEEKLY_DIGEST.value == "weekly_digest"
    assert Channel.COMMIT_REPORT.value == "commit_report"
    assert Channel("standup") == Channel.STANDUP


def test_weekly_digest_roundtrip():
    d = _digest()
    restored = WeeklyDigest.model_validate(d.model_dump())
    assert restored.highlights == ["Shipped M5"]
    assert restored.next_week == ["M6 CI/CD"]


def test_draft_payload_auto_id():
    draft = DraftPayload[WeeklyDigest](
        artifact_type=Channel.WEEKLY_DIGEST,
        payload=_digest(),
        developer_id="dev-1",
    )
    assert draft.draft_id  # UUID auto-generated
    assert draft.developer_id == "dev-1"


def test_draft_payload_roundtrip():
    draft = DraftPayload[WeeklyDigest](
        artifact_type=Channel.WEEKLY_DIGEST,
        payload=_digest(),
        developer_id="dev-1",
    )
    restored = DraftPayload[WeeklyDigest].model_validate(draft.model_dump())
    assert restored.draft_id == draft.draft_id


def test_approved_payload_defaults():
    ap = ApprovedPayload[WeeklyDigest](
        draft_id="draft-abc",
        artifact_type=Channel.WEEKLY_DIGEST,
        payload=_digest(),
        approval_source=ApprovalSource.MANUAL,
        developer_id="dev-1",
    )
    assert ap.redaction_level == RedactionLevel.STANDARD
    assert ap.approval_id  # auto-generated


def test_approved_payload_roundtrip():
    ap = ApprovedPayload[WeeklyDigest](
        draft_id="draft-abc",
        artifact_type=Channel.WEEKLY_DIGEST,
        payload=_digest(),
        approval_source=ApprovalSource.POLICY,
        developer_id="dev-1",
    )
    restored = ApprovedPayload[WeeklyDigest].model_validate(ap.model_dump())
    assert restored.approval_source == ApprovalSource.POLICY
    assert restored.payload.highlights == ["Shipped M5"]


def test_subscriber_identity_is_active():
    sub = SubscriberIdentity(
        name="alice",
        receive_url="http://server:7475",
        channels=[Channel.WEEKLY_DIGEST],
    )
    assert sub.is_active is True
    assert sub.subscriber_id.startswith("sub_")
    assert sub.revoked_at is None


def test_subscriber_identity_revoked():
    sub = SubscriberIdentity(
        name="bob",
        receive_url="http://server:7475",
        revoked_at=_NOW,
    )
    assert sub.is_active is False


def test_sharing_policy_defaults():
    policy = SharingPolicy(subscriber_id="sub_abc123")
    assert policy.auto_approve is False
    assert policy.redaction_level == RedactionLevel.STANDARD
    assert policy.probe_hours_start is None
    assert policy.probe_repos_allow == []


def test_standup_bullets_no_blockers():
    bullets = StandupBullets(
        yesterday=["Finished M5", "Wrote tests"],
        today=["Start M6"],
    )
    assert bullets.blockers == []


def test_pr_draft_fields():
    pr = PRDraft(
        branch="feature/m6-release",
        base="main",
        title="M6: Distribution & CI/CD",
        summary="Adds GitHub Actions pipelines and PyPI publishing",
        rationale="Makes v1 shippable",
        test_notes="pytest suite passes",
        risk_notes="none",
    )
    assert pr.title == "M6: Distribution & CI/CD"
    assert pr.base == "main"
