"""Semantic collision detector — embedding-based pre-commit radar.

Complementary to the file-overlap `CollisionDetector` in detector.py:
    - File detector catches "Alice edits auth.py while Bob also edits auth.py".
    - Semantic detector catches "Alice refactoring auth middleware" + "Bob
      rewriting session validation" when the files diverge but the *concept*
      overlaps.

Pulls recent artifacts with vectors from `ReceivedStore` and does Python-level
pairwise cosine. Threshold defaults to 0.82; shared TF-IDF-ish terms are
reported alongside each hit so the manager can see *why* a pair collided.
"""
from __future__ import annotations

import json
import logging
import math
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from vantage.schemas import SemanticCollision

if TYPE_CHECKING:
    from shadow_replica.storage.received_store import ReceivedArtifactRow, ReceivedStore
    from shadow_replica.config import TrustedDev

logger = logging.getLogger(__name__)

_DEFAULT_THRESHOLD = 0.82
_HIGH_THRESHOLD = 0.92        # severity bumps to "high" above this
_MIN_SHARED_TERMS = 2         # need at least 2 overlapping salient terms OR a file-overlap
_IGNORED_TYPES = frozenset({"friction_report", "live_probe"})

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-]{2,}")
_STOPWORDS = frozenset("""
a an and are as at be been being but by can could did do does doing done for
from had has have having here if in into is it its of on or over so still such
that the their them then there these they this those through to too under until
up upon very was we were what when where which while who whom why will with
you your yours
add adds added adding update updates updated updating fix fixes fixed fixing
make makes made use uses used using get gets got getting
also however only still now just more some many new old small big
run runs ran running start starts started
yes no ok done todo wip
pr prs commit commits push pushed pushing push
file files code codes line lines
""".split())


# ── Vector helpers ──────────────────────────────────────────────────────────

def _is_zero(vec: list[float] | None) -> bool:
    return not vec or all(v == 0.0 for v in vec)


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


# ── Text / term helpers ────────────────────────────────────────────────────

def _tokens(text: str) -> set[str]:
    out: set[str] = set()
    for m in _WORD_RE.finditer(text.lower()):
        w = m.group(0)
        if len(w) < 3 or w in _STOPWORDS:
            continue
        out.add(w)
    return out


def _text_for_terms(artifact_type: str, payload: dict) -> str:
    """Pick the salient fields a human would use to describe this artifact."""
    bits: list[str] = []
    if artifact_type == "commit_report":
        for k in ("author_msg", "generated_summary", "branch"):
            v = payload.get(k)
            if v:
                bits.append(str(v))
        bits.extend(str(f) for f in (payload.get("files_touched") or [])[:5])
    elif artifact_type == "pr_draft":
        for k in ("title", "summary", "rationale", "branch"):
            v = payload.get(k)
            if v:
                bits.append(str(v))
    elif artifact_type == "github_event":
        for k in ("title", "summary", "branch", "raw_payload_excerpt"):
            v = payload.get(k)
            if v:
                bits.append(str(v))
    elif artifact_type == "standup":
        for k in ("today", "yesterday", "blockers"):
            for v in (payload.get(k) or [])[:4]:
                bits.append(str(v))
    elif artifact_type == "weekly_digest":
        for k in ("highlights", "open_threads", "next_week"):
            for v in (payload.get(k) or [])[:4]:
                bits.append(str(v))
    elif artifact_type == "quick_note":
        v = payload.get("message")
        if v:
            bits.append(str(v))
    elif artifact_type == "bug_resolution":
        for k in ("error_signature", "fix_summary"):
            v = payload.get(k)
            if v:
                bits.append(str(v))
    else:
        for k in ("title", "summary", "text", "message"):
            v = payload.get(k)
            if v:
                bits.append(str(v))
    return " · ".join(b for b in bits if b)[:800]


def _files_for(artifact_type: str, payload: dict) -> list[str]:
    """Best-effort file list from the payload."""
    files = payload.get("files_touched") or payload.get("files") or []
    if isinstance(files, list):
        return [str(f) for f in files]
    return []


def _summary_for(artifact_type: str, payload: dict) -> str:
    if artifact_type == "github_event":
        return str(payload.get("summary") or payload.get("title") or "")[:140]
    if artifact_type == "commit_report":
        return str(payload.get("author_msg") or payload.get("generated_summary") or "")[:140]
    if artifact_type == "pr_draft":
        return str(payload.get("title") or "")[:140]
    if artifact_type == "quick_note":
        return str(payload.get("message") or "")[:140]
    if artifact_type == "bug_resolution":
        sig = str(payload.get("error_signature") or "")[:80]
        fix = str(payload.get("fix_summary") or "")[:40]
        return f"{sig} → {fix}" if sig else "bug resolution"
    if artifact_type == "standup":
        today = payload.get("today") or []
        return str(today[0])[:140] if today else "standup update"
    if artifact_type == "weekly_digest":
        hi = payload.get("highlights") or []
        return str(hi[0])[:140] if hi else "weekly digest"
    return artifact_type


# ── Main detector ──────────────────────────────────────────────────────────

class SemanticCollisionDetector:
    """Scan recent cross-developer artifacts for high cosine-similarity pairs."""

    def __init__(
        self,
        store: "ReceivedStore",
        threshold: float = _DEFAULT_THRESHOLD,
        high_threshold: float = _HIGH_THRESHOLD,
    ) -> None:
        self._store = store
        self._threshold = threshold
        self._high_threshold = high_threshold

    async def scan_recent(
        self,
        hours: int = 48,
        limit: int = 200,
        max_collisions: int = 20,
        dev_name_map: dict[str, str] | None = None,
    ) -> list[SemanticCollision]:
        """Return the top-N semantic collisions in the window.

        Deduplicates by (developer_a, developer_b) pair — only the highest-
        similarity artifact pair per dev-pair is reported, so one broad
        refactor doesn't spam N⨉N copies of the same alert.
        """
        rows_with_vecs = await self._store.list_recent_with_vectors(hours=hours, limit=limit)
        dev_name_map = dev_name_map or {}

        # Pre-compute per-artifact text / tokens / files / vector / dev_id
        prepped: list[dict] = []
        for row, vec in rows_with_vecs:
            atype = row.artifact_type
            if atype in _IGNORED_TYPES:
                continue
            try:
                payload = json.loads(row.payload_json or "{}")
            except Exception:
                continue
            text = _text_for_terms(atype, payload)
            if not text:
                continue
            tokens = _tokens(text)
            prepped.append({
                "approval_id": row.approval_id,
                "developer_id": row.developer_id,
                "artifact_type": atype,
                "payload": payload,
                "vector": vec,
                "tokens": tokens,
                "files": _files_for(atype, payload),
                "summary": _summary_for(atype, payload),
            })

        if len(prepped) < 2:
            return []

        # Dev-pair → (best collision so far)
        best_per_pair: dict[tuple[str, str], SemanticCollision] = {}
        for i in range(len(prepped)):
            a = prepped[i]
            for j in range(i + 1, len(prepped)):
                b = prepped[j]
                if a["developer_id"] == b["developer_id"]:
                    continue
                if a["developer_id"].startswith("unmapped:") or b["developer_id"].startswith("unmapped:"):
                    # Unmapped github events can't be attributed to a session —
                    # skip them for collision detection (noisy and uninformative).
                    continue

                shared_files = sorted(set(a["files"]) & set(b["files"]))
                shared_terms = sorted(a["tokens"] & b["tokens"])

                sim = _cosine(a["vector"], b["vector"]) if not _is_zero(a["vector"]) and not _is_zero(b["vector"]) else 0.0

                # To fire we require ONE of:
                #   (a) cosine >= threshold AND >= _MIN_SHARED_TERMS tokens (guard against noise), OR
                #   (b) ANY file overlap (that's an unambiguous collision regardless of cosine)
                if not shared_files:
                    if sim < self._threshold:
                        continue
                    if len(shared_terms) < _MIN_SHARED_TERMS:
                        continue

                severity = "high" if (sim >= self._high_threshold or len(shared_files) >= 2) else "medium"
                if sim < self._threshold and shared_files and not shared_terms:
                    severity = "low"

                pair_key = tuple(sorted((a["developer_id"], b["developer_id"])))
                # Keep the higher-similarity pair per dev-pair.
                existing = best_per_pair.get(pair_key)
                if existing is not None and existing.cosine_similarity >= sim and not shared_files:
                    continue

                dev_a_id, dev_b_id = pair_key
                a_is_first = (dev_a_id == a["developer_id"])
                side_a = a if a_is_first else b
                side_b = b if a_is_first else a

                best_per_pair[pair_key] = SemanticCollision(
                    developer_a=dev_a_id,
                    developer_b=dev_b_id,
                    developer_a_name=dev_name_map.get(dev_a_id, dev_a_id),
                    developer_b_name=dev_name_map.get(dev_b_id, dev_b_id),
                    artifact_a_id=side_a["approval_id"],
                    artifact_b_id=side_b["approval_id"],
                    artifact_a_type=side_a["artifact_type"],
                    artifact_b_type=side_b["artifact_type"],
                    artifact_a_summary=side_a["summary"],
                    artifact_b_summary=side_b["summary"],
                    cosine_similarity=round(sim, 4),
                    shared_terms=shared_terms[:10],
                    file_overlap=shared_files[:10],
                    severity=severity,
                )

        collisions = list(best_per_pair.values())
        collisions.sort(
            key=lambda c: (c.cosine_similarity, len(c.file_overlap), len(c.shared_terms)),
            reverse=True,
        )
        return collisions[:max_collisions]
