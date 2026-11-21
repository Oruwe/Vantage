from ghost_daemon.redaction.scrubber import scrub, scrub_payload

# Valid AWS access key: AKIA + exactly 16 uppercase alphanumeric chars
_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
# GitHub personal access token: ghp_ + 36+ alphanumeric chars
_GH_TOKEN = "ghp_" + "A" * 36
# Anthropic API key
_ANTHROPIC_KEY = "sk-ant-api01-" + "B" * 25
# JWT with three base64url segments starting with eyJ
_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
    ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)


def test_redacts_aws_access_key():
    ctx = scrub(f"export AWS_ACCESS_KEY_ID={_AWS_KEY}")
    assert _AWS_KEY not in ctx.scrubbed_text
    assert any(r.kind == "aws_access_key" for r in ctx.redactions)


def test_redacts_github_token():
    ctx = scrub(f"GITHUB_TOKEN={_GH_TOKEN}")
    assert _GH_TOKEN not in ctx.scrubbed_text
    assert any(r.kind == "github_token" for r in ctx.redactions)


def test_redacts_anthropic_key():
    ctx = scrub(f"api_key = '{_ANTHROPIC_KEY}'")
    assert _ANTHROPIC_KEY not in ctx.scrubbed_text
    assert any(r.kind == "anthropic_key" for r in ctx.redactions)


def test_redacts_jwt():
    ctx = scrub(f"Authorization: Bearer {_JWT}")
    assert "eyJhbGci" not in ctx.scrubbed_text


def test_clean_text_unchanged():
    text = "This is a normal log message with no secrets."
    ctx = scrub(text)
    assert ctx.scrubbed_text == text
    assert ctx.redactions == []


def test_original_length_recorded():
    text = f"token={_GH_TOKEN}"
    ctx = scrub(text)
    assert ctx.original_length == len(text)


def test_multiple_secrets_in_one_string():
    text = f"key={_AWS_KEY} and token={_GH_TOKEN}"
    ctx = scrub(text)
    assert _AWS_KEY not in ctx.scrubbed_text
    assert _GH_TOKEN not in ctx.scrubbed_text
    assert len(ctx.redactions) >= 2


def test_entropy_bypass_for_lockfile():
    # A high-entropy string that matches no named pattern should be left alone
    # in lock files (entropy check is suppressed for *.lock paths)
    high_entropy = "xK8mP2nQvR6wT9yU3jL0fH7cD1gB5eA4"  # 32 chars, mixed, high entropy
    text = f"  hash = {high_entropy}"
    ctx = scrub(text, file_path="uv.lock")
    assert high_entropy in ctx.scrubbed_text
    assert not any(r.kind == "high_entropy" for r in ctx.redactions)


def test_file_path_recorded():
    ctx = scrub("hello world", file_path="src/main.py")
    assert ctx.file_path == "src/main.py"


def test_scrub_payload_walks_nested_strings():
    payload = {
        "summary": f"deploy with {_AWS_KEY}",
        "bullets": ["fine line", f"paste accident: {_GH_TOKEN}"],
        "meta": {"token": _ANTHROPIC_KEY, "count": 3, "ok": True},
    }
    scrubbed, redactions = scrub_payload(payload)

    kinds = {r.kind for r in redactions}
    assert {"aws_access_key", "github_token", "anthropic_key"} <= kinds
    # Original secrets must be gone from every leaf.
    flat = repr(scrubbed)
    assert _AWS_KEY not in flat
    assert _GH_TOKEN not in flat
    assert _ANTHROPIC_KEY not in flat
    # Non-string leaves are preserved untouched.
    assert scrubbed["meta"]["count"] == 3
    assert scrubbed["meta"]["ok"] is True


def test_approval_gate_rescrubs_edited_payload(tmp_path):
    from ghost_daemon.approval.gate import ApprovalGate
    from ghost_daemon.storage.sqlite_store import SQLiteStore
    from vantage.schemas import DraftPayload, QuickNote

    store = SQLiteStore(tmp_path / "vantage.db")
    gate = ApprovalGate(store, "alice")

    clean = DraftPayload(
        draft_id="d1",
        artifact_type="quick_note",
        developer_id="alice",
        payload=QuickNote(message="shipped auth refactor"),
    )
    gate.submit(clean)

    # Dev edits the draft and (accidentally) pastes a secret before approving.
    approved = gate.approve(
        "d1",
        edited_payload={"message": f"shipped with key {_AWS_KEY}"},
    )

    assert _AWS_KEY not in approved.payload.message
    assert "[REDACTED:aws_access_key" in approved.payload.message
