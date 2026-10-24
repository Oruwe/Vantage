from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass
from fnmatch import fnmatch

from vantage.schemas import Redaction, RedactedContext

# Files where the generic high-entropy check is disabled to avoid mangling
# lockfile hashes, minified code, and binary assets.
_ENTROPY_BYPASS_GLOBS = frozenset({
    "*.lock", "yarn.lock", "package-lock.json", "cargo.lock",
    "poetry.lock", "uv.lock", "pipfile.lock", "gemfile.lock",
    "*.min.js", "*.min.css", "*.map",
    "*.svg", "*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp",
    "*.ico", "*.woff", "*.woff2", "*.ttf", "*.eot", "*.otf",
    "*.pdf", "*.wasm", "*.bin", "*.pyc", "*.pyo",
    "*.exe", "*.dll", "*.so", "*.dylib", "*.a", "*.lib",
})

# (kind, compiled_pattern) — applied in order; first match wins per position
_NAMED_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("private_key",   re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |PGP |DSA )?PRIVATE KEY-----.*?-----END[^-]+-----",
        re.DOTALL,
    )),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("aws_secret_key", re.compile(
        r"(?i)(?:aws.{0,20}secret|secret.{0,20}aws).{0,10}[=:]\s*[A-Za-z0-9/+]{40}\b"
    )),
    ("github_token",  re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("gitlab_token",  re.compile(r"\bglpat-[A-Za-z0-9\-_]{20,}\b")),
    ("slack_token",   re.compile(r"\bxox[baprs]-[0-9A-Za-z\-]{10,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{20,}\b")),
    ("openai_key",    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("stripe_live",   re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{20,}\b")),
    ("gcp_api_key",   re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b")),
    ("azure_acct_key", re.compile(r"AccountKey=[A-Za-z0-9+/]{43,}={0,2}")),
    ("jwt",           re.compile(
        r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"
    )),
    ("basic_auth_url", re.compile(r"https?://[^:\s]+:[^@\s]{4,}@\S+")),
    ("bearer_token",  re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/]+=*\b")),
    ("dotenv_secret", re.compile(
        r"^[A-Z][A-Z0-9_]{2,}=[^\s'\"]{8,}$", re.MULTILINE
    )),
]

# Matches binary diff markers to skip entropy on binary hunks
_BINARY_DIFF_RE = re.compile(r"GIT binary patch|Binary files .* differ")


def _placeholder(kind: str, secret: str) -> str:
    h = hashlib.sha256(secret.encode("utf-8", errors="replace")).hexdigest()[:6]
    return f"[REDACTED:{kind}:{h}]"


def _shannon_entropy(s: str) -> float:
    n = len(s)
    if n == 0:
        return 0.0
    counts = Counter(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _is_hex_like(s: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F\-]{20,}", s))


def _should_bypass_entropy(file_path: str | None) -> bool:
    if not file_path:
        return False
    name = re.split(r"[/\\]", file_path)[-1].lower()
    return any(fnmatch(name, pat) for pat in _ENTROPY_BYPASS_GLOBS)


def _find_high_entropy(text: str) -> list[tuple[int, int, str]]:
    hits: list[tuple[int, int, str]] = []
    for m in re.finditer(r"[A-Za-z0-9+/=_\-]{24,}", text):
        token = m.group()
        if _is_hex_like(token):
            continue
        if _shannon_entropy(token) > 4.7:
            hits.append((m.start(), m.end(), token))
    return hits


def scrub(text: str, file_path: str | None = None) -> RedactedContext:
    original_length = len(text)
    redactions: list[Redaction] = []
    result = text

    # Apply named patterns first (always, regardless of file_path)
    for kind, pattern in _NAMED_PATTERNS:
        def _replacer(m: re.Match, _kind: str = kind) -> str:
            secret = m.group()
            ph = _placeholder(_kind, secret)
            redactions.append(Redaction(kind=_kind, placeholder=ph, position=m.start()))
            return ph

        result = pattern.sub(_replacer, result)

    # Apply entropy check only for non-bypassed paths and non-binary diffs
    if not _should_bypass_entropy(file_path) and not _BINARY_DIFF_RE.search(result):
        for start, end, token in _find_high_entropy(result):
            ph = _placeholder("high_entropy", token)
            redactions.append(Redaction(kind="high_entropy", placeholder=ph, position=start))
            result = result[:start] + ph + result[end:]

    return RedactedContext(
        original_length=original_length,
        scrubbed_text=result,
        redactions=redactions,
        file_path=file_path,
    )


def scrub_turns(turns: list[dict]) -> list[dict]:
    """Scrub a list of raw session turn dicts in place (returns new list)."""
    out = []
    for turn in turns:
        content = turn.get("content_excerpt", "") or turn.get("content", "")
        if isinstance(content, str) and content:
            ctx = scrub(content)
            turn = {**turn, "content_excerpt": ctx.scrubbed_text}
        out.append(turn)
    return out
