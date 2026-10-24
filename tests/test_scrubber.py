from ghost_daemon.redaction.scrubber import scrub

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
