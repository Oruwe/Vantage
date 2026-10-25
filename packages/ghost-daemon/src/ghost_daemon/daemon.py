from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import uvicorn


@dataclass
class PairingState:
    """Live connection state for one subscriber's outbound WS link."""
    subscriber_id: str
    name: str
    connected: bool = False
    connected_at: str = ""         # ISO timestamp of current connection (if connected)
    last_disconnect_at: str = ""   # ISO timestamp of most recent disconnect
    last_error: str = ""           # last error message, empty when healthy
    attempt_count: int = 0         # total attempt count — bumps on each (re)connect try


class PairingStatus:
    """Thread-safe-ish mutable record of each subscriber's WS state.

    Mutated from WSClient callbacks (async context) and read by the local UI
    endpoint. Mutations here are single-attribute dict writes under the GIL,
    so no explicit lock is needed for the small surface we expose.
    """

    def __init__(self) -> None:
        self._states: dict[str, PairingState] = {}

    def register(self, subscriber_id: str, name: str) -> None:
        if subscriber_id not in self._states:
            self._states[subscriber_id] = PairingState(subscriber_id=subscriber_id, name=name)

    def mark_connected(self, subscriber_id: str) -> None:
        s = self._states.get(subscriber_id)
        if not s:
            return
        s.connected = True
        s.connected_at = datetime.now(timezone.utc).isoformat()
        s.last_error = ""
        s.attempt_count += 1

    def mark_disconnected(self, subscriber_id: str, error: str = "") -> None:
        s = self._states.get(subscriber_id)
        if not s:
            return
        s.connected = False
        s.last_disconnect_at = datetime.now(timezone.utc).isoformat()
        if error:
            s.last_error = error[:200]

    def mark_attempt(self, subscriber_id: str) -> None:
        s = self._states.get(subscriber_id)
        if s:
            s.attempt_count += 1

    def snapshot(self) -> list[dict]:
        return [
            {
                "subscriber_id": s.subscriber_id,
                "name": s.name,
                "connected": s.connected,
                "connected_at": s.connected_at,
                "last_disconnect_at": s.last_disconnect_at,
                "last_error": s.last_error,
                "attempt_count": s.attempt_count,
            }
            for s in self._states.values()
        ]

from ghost_daemon.audit.log import AuditLog
from ghost_daemon.approval.gate import ApprovalGate
from ghost_daemon.config import Config
from ghost_daemon.models.registry import build_from_config
from ghost_daemon.storage.sqlite_store import SQLiteStore
from ghost_daemon.storage.qdrant_store import QdrantStore
from ghost_daemon.subscribers.registry import SubscriberRegistry
from ghost_daemon.ui.local_app import build_app
from ghost_daemon.watchers.project_map import ProjectMap
from ghost_daemon.watchers.session_tail import SessionTailer

logger = logging.getLogger(__name__)


def _artifact_text(artifact_type: str, payload) -> str:
    """Extract a text representation of an artifact for embedding."""
    try:
        if artifact_type == "weekly_digest":
            highlights = getattr(payload, "highlights", [])
            commits = getattr(payload, "commits", [])
            return " ".join(highlights[:5] + commits[:3])
        if artifact_type == "commit_report":
            return (
                f"{getattr(payload, 'author_msg', '')} "
                f"{getattr(payload, 'generated_summary', '')} "
                f"{' '.join(getattr(payload, 'files_touched', [])[:5])}"
            )
        if artifact_type == "standup":
            return " ".join(
                getattr(payload, "yesterday", [])[:3]
                + getattr(payload, "today", [])[:3]
            )
        if artifact_type == "pr_draft":
            return (
                f"{getattr(payload, 'title', '')} "
                f"{getattr(payload, 'summary', '')} "
                f"{getattr(payload, 'rationale', '')}"
            )
        if artifact_type == "quick_note":
            return getattr(payload, "message", str(payload))
    except Exception:
        pass
    return str(payload)[:500]


class GhostDaemon:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.store = SQLiteStore(cfg.sqlite_path)
        self.qdrant = QdrantStore(url=cfg.qdrant.url) if cfg.qdrant.enabled else QdrantStore()
        self.audit_log = AuditLog(cfg.audit_dir, cfg.audit.retention_days)
        self.project_map = ProjectMap(cfg)
        self.tailer = SessionTailer(cfg.state_dir)
        self.llm = build_from_config(cfg)
        self.gate = ApprovalGate(self.store, cfg.developer_id)
        self.subscriber_registry = SubscriberRegistry(cfg.sqlite_path)
        self.pairing_status = PairingStatus()
        self._app = build_app(
            self.store, self.gate, self.audit_log, self.subscriber_registry, cfg,
            qdrant=self.qdrant, llm=self.llm,
            pairing_status=self.pairing_status,
        )

    async def start(self) -> None:
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.audit_dir.mkdir(parents=True, exist_ok=True)
        self.project_map.refresh()
        logger.info(
            "Ghost daemon started (developer_id=%s, ui=http://%s:%d)",
            self.cfg.developer_id,
            self.cfg.ui.bind_address,
            self.cfg.ui.port,
        )

    async def run_forever(self) -> None:
        await self.start()

        tasks: list[asyncio.Task] = []

        live_probe_subs = self.subscriber_registry.subscribers_for_channel("live_probe")
        if live_probe_subs:
            from ghost_daemon.agents.discovery_agent import DiscoveryAgent
            from ghost_daemon.agents.probe_agent import ProbeAgent
            from ghost_daemon.transport.ws_client import WSClient

            probe_agent = ProbeAgent(self.cfg, self.llm, self.tailer, store=self.store)
            discovery_agent = DiscoveryAgent(self.cfg, self.llm, self.tailer)
            machine_id = self.cfg.machine_id

            for row in live_probe_subs:
                identity = self.subscriber_registry.to_identity(row)
                self.pairing_status.register(identity.subscriber_id, identity.name)

                def _on_connect(sid=identity.subscriber_id):
                    self.pairing_status.mark_connected(sid)

                def _on_disconnect(err="", sid=identity.subscriber_id):
                    self.pairing_status.mark_disconnected(sid, err)

                client = WSClient(
                    identity, self.cfg.developer_id, machine_id,
                    probe_agent, discovery_agent=discovery_agent,
                    tailer=self.tailer, cfg=self.cfg,
                    on_connect=_on_connect,
                    on_disconnect=_on_disconnect,
                )
                tasks.append(asyncio.create_task(
                    client.run_forever(),
                    name=f"ws_client:{row.id}",
                ))
            logger.info(
                "Started %d WS client(s) for live_probe subscribers",
                len(live_probe_subs),
            )

        if self.cfg.schedule.standup or self.cfg.schedule.digest:
            from ghost_daemon.scheduler import Scheduler
            scheduler = Scheduler(self.cfg, self)
            tasks.append(asyncio.create_task(scheduler.run(), name="scheduler"))
            logger.info("Scheduler task started")

        tasks.append(asyncio.create_task(self._dejavu_scan_loop(), name="dejavu_scan"))
        logger.info("Deja-Vu scanner started")

        config = uvicorn.Config(
            app=self._app,
            host=self.cfg.ui.bind_address,
            port=self.cfg.ui.port,
            log_level="warning",
        )
        server = uvicorn.Server(config)
        try:
            await server.serve()
        finally:
            for t in tasks:
                t.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def index_approved(self, approved) -> None:
        """Embed and store an approved artifact in Qdrant for historical context retrieval."""
        try:
            import json as _json
            payload = approved.payload
            text = _artifact_text(approved.artifact_type, payload)
            vec = await self.llm.embed(text)
            payload_dict = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else {}
            await self.qdrant.upsert_approved(
                approved_id=approved.approval_id,
                developer_id=approved.developer_id,
                artifact_type=approved.artifact_type,
                payload=payload_dict,
                text=text,
                vector=vec,
                approved_at=approved.approved_at.isoformat(),
            )
        except Exception as exc:
            logger.debug("Qdrant index_approved skipped: %s", exc)

    def get_app(self):
        return self._app

    async def _dejavu_scan_loop(self) -> None:
        """Every 15 minutes, scan recent session turns for struggle-then-success
        patterns and submit any new BugResolution to the gate.

        De-dupes by error_signature against the recent-drafts table so we don't
        resubmit the same resolution over and over.
        """
        from ghost_daemon.agents.dejavu_detector import detect_resolutions
        from vantage.schemas import DraftPayload

        interval_s = 900  # 15 minutes
        while True:
            try:
                await asyncio.sleep(interval_s)
                turns = []
                for session_dir in self.cfg.resolved_session_dirs:
                    jsonl_files = sorted(
                        session_dir.rglob("*.jsonl"),
                        key=lambda p: p.stat().st_mtime,
                        reverse=True,
                    )
                    for jsonl in jsonl_files[:3]:
                        try:
                            turns.extend(await self.tailer.tail_recent(jsonl, n=60))
                        except Exception:
                            continue
                    if len(turns) >= 60:
                        break
                turns = turns[:60]
                if not turns:
                    continue

                # Pull recent drafts to de-dupe
                recent = self.store.list_recent(limit=50)
                recent_sigs: set[str] = set()
                for r in recent:
                    if r.artifact_type != "bug_resolution":
                        continue
                    try:
                        import json as _j
                        src = r.edited_payload_json or r.payload_json
                        payload = _j.loads(src)
                        sig = payload.get("error_signature", "")
                        if sig:
                            recent_sigs.add(sig)
                    except Exception:
                        continue

                resolutions = detect_resolutions(
                    turns, developer_id=self.cfg.developer_id, repo="",
                )
                for res in resolutions:
                    if res.error_signature in recent_sigs:
                        continue
                    draft = DraftPayload(
                        artifact_type="bug_resolution",
                        payload=res,
                        developer_id=self.cfg.developer_id,
                    )
                    self.gate.submit(draft)
                    logger.info(
                        "Deja-Vu: submitted bug_resolution draft for sig=%r (attempts=%d)",
                        res.error_signature[:60], res.attempt_count,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Deja-Vu scan blip: %s", exc)
