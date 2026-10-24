from __future__ import annotations

import asyncio
import logging

import uvicorn

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


class GhostDaemon:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.store = SQLiteStore(cfg.sqlite_path)
        self.qdrant = QdrantStore()
        self.audit_log = AuditLog(cfg.audit_dir, cfg.audit.retention_days)
        self.project_map = ProjectMap(cfg)
        self.tailer = SessionTailer(cfg.state_dir)
        self.llm = build_from_config(cfg)
        self.gate = ApprovalGate(self.store, cfg.developer_id)
        self.subscriber_registry = SubscriberRegistry(cfg.sqlite_path)
        self._app = build_app(self.store, self.gate, self.audit_log, self.subscriber_registry, cfg)

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
            from ghost_daemon.agents.probe_agent import ProbeAgent
            from ghost_daemon.transport.ws_client import WSClient

            probe_agent = ProbeAgent(self.cfg, self.llm, self.tailer)
            machine_id = self.cfg.machine_id

            for row in live_probe_subs:
                identity = self.subscriber_registry.to_identity(row)
                client = WSClient(identity, self.cfg.developer_id, machine_id, probe_agent)
                tasks.append(asyncio.create_task(
                    client.run_forever(),
                    name=f"ws_client:{row.id}",
                ))
            logger.info(
                "Started %d WS client(s) for live_probe subscribers",
                len(live_probe_subs),
            )

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

    def get_app(self):
        return self._app
