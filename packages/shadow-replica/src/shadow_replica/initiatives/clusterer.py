"""Initiative clustering: group team artifacts into live themes.

Given a batch of commits/standups/digests/PR drafts from across the team, this
module clusters them into `Initiative` records keyed by semantic (embeddings)
or lexical (Jaccard token-overlap) similarity. Each initiative gets:

- a short human-readable label (TF-IDF salient terms, filtered against a
  stopword list — no LLM required for V1)
- the set of contributing developers
- shipped / in-flight / blocker bullets extracted from the underlying payloads
- a chronological timeline of events
- a momentum score reflecting recency × contributor count

The embedder is optional. If `embed_vectors` is None or any vector is a
zero-vector, we fall back to lexical similarity so clustering degrades
gracefully rather than collapsing into one giant cluster.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from vantage.schemas import Initiative, InitiativeEvent

if TYPE_CHECKING:
    from shadow_replica.storage.received_store import ReceivedArtifactRow

logger = logging.getLogger(__name__)

_SIM_THRESHOLD_EMBED = 0.65
_SIM_THRESHOLD_LEX = 0.18
_LABEL_TOP_N = 3

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
""".split())

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-]{2,}")


# ── Text extraction ─────────────────────────────────────────────────────────

def extract_text(artifact_type: str, payload: dict) -> str:
    """Pull the semantic content out of a payload — the strings that would
    make this artifact cluster with other artifacts about the same theme.

    Intentionally terse (<=600 chars) so the embedder stays cheap.
    """
    parts: list[str] = []
    if artifact_type == "commit_report":
        for k in ("author_msg", "generated_summary", "branch"):
            v = payload.get(k)
            if v:
                parts.append(str(v))
        for f in payload.get("files_touched", [])[:6]:
            parts.append(str(f))
    elif artifact_type == "weekly_digest":
        for k in ("highlights", "open_threads", "next_week"):
            for v in (payload.get(k) or [])[:4]:
                parts.append(str(v))
    elif artifact_type == "standup":
        for k in ("today", "yesterday", "blockers"):
            for v in (payload.get(k) or [])[:3]:
                parts.append(str(v))
    elif artifact_type == "pr_draft":
        for k in ("title", "summary", "rationale", "branch"):
            v = payload.get(k)
            if v:
                parts.append(str(v))
    elif artifact_type == "quick_note":
        v = payload.get("message")
        if v:
            parts.append(str(v))
    elif artifact_type == "bug_resolution":
        for k in ("error_signature", "fix_summary"):
            v = payload.get(k)
            if v:
                parts.append(str(v))
    elif artifact_type == "github_event":
        for k in ("title", "summary", "branch", "repo", "raw_payload_excerpt"):
            v = payload.get(k)
            if v:
                parts.append(str(v))
    else:
        # Generic fallback — stringify a handful of promising keys
        for k in ("title", "summary", "text", "message"):
            v = payload.get(k)
            if v:
                parts.append(str(v))
    joined = " · ".join(p for p in parts if p)
    return joined[:600]


# ── Similarity ──────────────────────────────────────────────────────────────

def _tokenize(text: str) -> set[str]:
    tokens = set()
    for m in _WORD_RE.finditer(text.lower()):
        w = m.group(0)
        if len(w) < 3 or w in _STOPWORDS:
            continue
        tokens.add(w)
    return tokens


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    return inter / len(a | b)


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


# ── Row preparation ─────────────────────────────────────────────────────────

@dataclass
class _Item:
    row_idx: int
    developer_id: str
    developer_name: str
    artifact_type: str
    approval_id: str
    received_at: datetime | None
    text: str
    tokens: set[str]
    vector: list[float] | None
    payload: dict


@dataclass
class _Cluster:
    members: list[_Item] = field(default_factory=list)
    centroid_vec: list[float] | None = None
    tokens: set[str] = field(default_factory=set)

    def add(self, item: _Item) -> None:
        self.members.append(item)
        self.tokens |= item.tokens
        if item.vector and not _is_zero(item.vector):
            if self.centroid_vec is None:
                self.centroid_vec = list(item.vector)
            else:
                n = len(self.members)
                self.centroid_vec = [
                    (c * (n - 1) + v) / n
                    for c, v in zip(self.centroid_vec, item.vector)
                ]


# ── Public API ──────────────────────────────────────────────────────────────

def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        s2 = s.replace("Z", "+00:00") if s.endswith("Z") else s
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _slug(label: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "unnamed"
    tail = hashlib.sha1(label.encode("utf-8")).hexdigest()[:6]
    return f"{base}-{tail}"


async def cluster_recent(
    rows: list["ReceivedArtifactRow"],
    dev_name_map: dict[str, str],
    *,
    period_start: datetime,
    period_end: datetime,
    embedder: Any | None = None,
) -> list[Initiative]:
    """Group the given rows into initiatives.

    `rows` must be the raw ReceivedArtifactRow objects (or compatible dicts).
    If an embedder is supplied and the backend is live, we use cosine similarity
    over embeddings; otherwise we fall back to token-set Jaccard.
    """
    items: list[_Item] = []
    for i, row in enumerate(rows):
        try:
            payload = _row_payload(row)
        except Exception:
            continue
        atype = _attr(row, "artifact_type")
        if atype in ("live_probe", "friction_report"):
            continue  # ephemeral — not a work artifact
        text = extract_text(atype, payload)
        if not text:
            continue
        dev_id = _attr(row, "developer_id")
        received = _parse_iso(_attr(row, "received_at"))
        items.append(_Item(
            row_idx=i,
            developer_id=dev_id,
            developer_name=dev_name_map.get(dev_id, dev_id),
            artifact_type=atype,
            approval_id=_attr(row, "approval_id"),
            received_at=received,
            text=text,
            tokens=_tokenize(text),
            vector=None,
            payload=payload,
        ))

    if embedder is not None:
        for it in items:
            try:
                it.vector = await embedder.embed(it.text)
            except Exception:
                it.vector = None

    use_embed = any(it.vector and not _is_zero(it.vector) for it in items)
    threshold = _SIM_THRESHOLD_EMBED if use_embed else _SIM_THRESHOLD_LEX

    clusters: list[_Cluster] = []
    # Walk oldest → newest so clusters anchor to earliest work.
    items.sort(key=lambda it: it.received_at or datetime.min.replace(tzinfo=timezone.utc))
    for it in items:
        best: _Cluster | None = None
        best_sim = 0.0
        for c in clusters:
            if use_embed and c.centroid_vec and it.vector and not _is_zero(it.vector):
                sim = _cosine(c.centroid_vec, it.vector)
            else:
                # Lexical mode: use the MAX pairwise Jaccard to any existing
                # member, not Jaccard against the union of all member tokens.
                # Union-based similarity decays as clusters grow because the
                # union token set keeps swelling with each new member; max-member
                # similarity captures "this item is close to at least one
                # member" which is the right semantics for an initiative.
                sim = max(
                    (_jaccard(m.tokens, it.tokens) for m in c.members),
                    default=0.0,
                )
            if sim > best_sim:
                best_sim = sim
                best = c
        if best is not None and best_sim >= threshold:
            best.add(it)
        else:
            new_c = _Cluster()
            new_c.add(it)
            clusters.append(new_c)

    initiatives: list[Initiative] = []
    for cluster in clusters:
        if not cluster.members:
            continue
        label, top_terms = _label(cluster)
        contributors = _dedupe_preserve([m.developer_id for m in cluster.members])
        contrib_names = _dedupe_preserve([m.developer_name for m in cluster.members])
        timeline, shipped, in_flight, blockers = _bullets(cluster)
        commit_count = sum(1 for m in cluster.members if m.artifact_type == "commit_report")
        earliest = min((m.received_at for m in cluster.members if m.received_at), default=None)
        latest = max((m.received_at for m in cluster.members if m.received_at), default=None)
        initiatives.append(Initiative(
            initiative_id=_slug(label),
            label=label,
            period_start=earliest or period_start,
            period_end=latest or period_end,
            contributors=contributors,
            contributor_names=contrib_names,
            artifact_count=len(cluster.members),
            commit_count=commit_count,
            shipped=shipped,
            in_flight=in_flight,
            blockers=blockers,
            top_terms=top_terms,
            timeline=timeline,
            momentum_score=_momentum(cluster.members, period_end),
            risk_flag=bool(blockers) or _is_cold(cluster.members, period_end),
        ))

    # Rank hot initiatives first
    initiatives.sort(key=lambda i: (i.momentum_score, i.artifact_count), reverse=True)
    return initiatives


# ── Helpers ─────────────────────────────────────────────────────────────────

def _row_payload(row) -> dict:
    if isinstance(row, dict):
        return row.get("payload", {}) or {}
    pj = getattr(row, "payload_json", None)
    if pj:
        return json.loads(pj)
    return {}


def _attr(row, name: str, default: str = "") -> str:
    if isinstance(row, dict):
        return row.get(name, default) or default
    return getattr(row, name, default) or default


def _dedupe_preserve(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for s in seq:
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _label(cluster: _Cluster) -> tuple[str, list[str]]:
    """TF-weighted salient-term extraction. Returns (label, top_terms)."""
    freq: dict[str, int] = {}
    for item in cluster.members:
        for tok in item.tokens:
            freq[tok] = freq.get(tok, 0) + 1
    scored = sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))
    top_terms = [w for w, _ in scored[:_LABEL_TOP_N] if freq[w] >= 1]
    if not top_terms:
        atype = cluster.members[0].artifact_type.replace("_", " ").title()
        return (f"Misc {atype}", [])
    # Prefer longer, more specific terms as the label anchor
    anchor = max(top_terms, key=len)
    rest = [w for w in top_terms if w != anchor]
    label_words = [anchor] + rest[:2]
    label = " ".join(w.replace("-", " ").replace("_", " ").title() for w in label_words)
    return label, top_terms


_BULLET_CAP = 4


def _bullets(cluster: _Cluster) -> tuple[list[InitiativeEvent], list[str], list[str], list[str]]:
    shipped: list[str] = []
    in_flight: list[str] = []
    blockers: list[str] = []
    timeline: list[InitiativeEvent] = []
    for item in sorted(cluster.members, key=lambda i: i.received_at or datetime.min.replace(tzinfo=timezone.utc)):
        p = item.payload
        if item.artifact_type == "commit_report":
            msg = (p.get("author_msg") or p.get("generated_summary") or "").strip()
            if msg:
                shipped.append(_tag(item, msg))
                timeline.append(_evt("shipped", msg, item))
        elif item.artifact_type == "weekly_digest":
            for h in (p.get("highlights") or [])[:3]:
                shipped.append(_tag(item, str(h)))
                timeline.append(_evt("shipped", str(h), item))
            for t in (p.get("open_threads") or [])[:3]:
                in_flight.append(_tag(item, str(t)))
                timeline.append(_evt("in_flight", str(t), item))
            for b in (p.get("blockers") or [])[:3]:
                blockers.append(_tag(item, str(b)))
                timeline.append(_evt("blocker", str(b), item))
        elif item.artifact_type == "standup":
            for t in (p.get("today") or [])[:3]:
                in_flight.append(_tag(item, str(t)))
                timeline.append(_evt("in_flight", str(t), item))
            for b in (p.get("blockers") or [])[:3]:
                blockers.append(_tag(item, str(b)))
                timeline.append(_evt("blocker", str(b), item))
        elif item.artifact_type == "pr_draft":
            title = (p.get("title") or "").strip()
            if title:
                in_flight.append(_tag(item, f"PR · {title}"))
                timeline.append(_evt("in_flight", f"PR · {title}", item))
            risk = (p.get("risk_notes") or "").strip()
            if risk:
                blockers.append(_tag(item, risk))
                timeline.append(_evt("blocker", risk, item))
        elif item.artifact_type == "bug_resolution":
            fix = p.get("fix_summary", "")
            if fix:
                shipped.append(_tag(item, f"fix: {fix}"))
                timeline.append(_evt("shipped", f"fix: {fix}", item))
        elif item.artifact_type == "github_event":
            event_type = p.get("event_type", "")
            summary = (p.get("summary") or "").strip()
            if not summary:
                continue
            if event_type == "pull_request.closed":
                excerpt = (p.get("raw_payload_excerpt") or "").lower()
                if "merged" in summary.lower() or "merged" in excerpt:
                    shipped.append(_tag(item, summary))
                    timeline.append(_evt("shipped", summary, item))
            elif event_type == "push":
                shipped.append(_tag(item, summary))
                timeline.append(_evt("shipped", summary, item))
            elif event_type.startswith("check_run.") or event_type.startswith("check_suite."):
                blockers.append(_tag(item, summary))
                timeline.append(_evt("blocker", summary, item))
            else:
                in_flight.append(_tag(item, summary))
                timeline.append(_evt("in_flight", summary, item))
    return (
        timeline,
        _dedupe_cap(shipped, _BULLET_CAP),
        _dedupe_cap(in_flight, _BULLET_CAP),
        _dedupe_cap(blockers, _BULLET_CAP),
    )


def _tag(item: _Item, text: str) -> str:
    name = item.developer_name or item.developer_id
    return f"{name}: {text[:140]}"


def _evt(kind: str, text: str, item: _Item) -> InitiativeEvent:
    return InitiativeEvent(
        kind=kind,
        text=text[:200],
        developer_id=item.developer_id,
        developer_name=item.developer_name,
        artifact_type=item.artifact_type,
        approval_id=item.approval_id,
        at=item.received_at,
    )


def _dedupe_cap(items: list[str], limit: int) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        key = it.lower()[:80]
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
        if len(out) >= limit:
            break
    return out


def _momentum(members: list[_Item], period_end: datetime) -> float:
    if not members:
        return 0.0
    contrib = len({m.developer_id for m in members})
    recency = 0.0
    for m in members:
        if not m.received_at:
            continue
        age_h = max(0.0, (period_end - m.received_at).total_seconds() / 3600.0)
        # 7-day half-life so this week dominates
        recency += 0.5 ** (age_h / 168.0)
    recency = min(recency / 6.0, 1.0)
    contrib_term = min(contrib / 4.0, 1.0)
    return round(0.6 * recency + 0.4 * contrib_term, 3)


def _is_cold(members: list[_Item], period_end: datetime) -> bool:
    """True if the most recent event is >48h old relative to period_end."""
    latest = max((m.received_at for m in members if m.received_at), default=None)
    if latest is None:
        return False
    return (period_end - latest) > timedelta(hours=48)
