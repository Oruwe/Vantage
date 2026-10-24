from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from vantage.schemas import ApprovalSource, AuditEntry, RedactedContext

logger = logging.getLogger(__name__)


class AuditLog:
    def __init__(self, audit_dir: Path, retention_days: int = 30) -> None:
        self._dir = audit_dir
        self._retention_days = retention_days
        self._dir.mkdir(parents=True, exist_ok=True)

    def _today_file(self) -> Path:
        return self._dir / f"{datetime.utcnow().date()}.jsonl"

    def record(
        self,
        *,
        kind: str,
        developer_id: str,
        context: RedactedContext,
        model_id: str,
        output: str,
        destination: str | None = None,
        approval_source: ApprovalSource = ApprovalSource.NONE,
    ) -> AuditEntry:
        original_hash = hashlib.sha256(
            context.scrubbed_text.encode("utf-8", errors="replace")
        ).hexdigest()

        entry = AuditEntry(
            kind=kind,
            developer_id=developer_id,
            destination=destination,
            pre_redaction_sha256=original_hash,
            redactions=context.redactions,
            scrubbed_input_excerpt=context.scrubbed_text[:2048],
            model=model_id,
            output=output[:4096],
            approval_source=approval_source,
        )
        self._append(entry)
        return entry

    def _append(self, entry: AuditEntry) -> None:
        line = entry.model_dump_json() + "\n"
        try:
            with open(self._today_file(), "a", encoding="utf-8") as f:
                f.write(line)
        except OSError as exc:
            logger.error("Could not write audit entry: %s", exc)

    def read_recent(self, days: int = 7) -> list[AuditEntry]:
        entries: list[AuditEntry] = []
        cutoff = datetime.utcnow().date() - timedelta(days=days)
        for jsonl in sorted(self._dir.glob("*.jsonl"), reverse=True):
            try:
                file_date = datetime.strptime(jsonl.stem, "%Y-%m-%d").date()
            except ValueError:
                continue
            if file_date < cutoff:
                break
            for line in jsonl.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    entries.append(AuditEntry.model_validate_json(line))
                except Exception:
                    pass
        return entries

    def purge_old(self) -> None:
        cutoff = datetime.utcnow().date() - timedelta(days=self._retention_days)
        for jsonl in self._dir.glob("*.jsonl"):
            try:
                file_date = datetime.strptime(jsonl.stem, "%Y-%m-%d").date()
                if file_date < cutoff:
                    jsonl.unlink()
                    logger.info("Purged old audit log %s", jsonl.name)
            except (ValueError, OSError):
                pass
