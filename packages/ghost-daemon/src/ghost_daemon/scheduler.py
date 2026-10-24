from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ghost_daemon.config import Config
    from ghost_daemon.daemon import GhostDaemon

logger = logging.getLogger(__name__)

_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def _parse_schedule(s: str) -> tuple[str, int, int] | None:
    """Parse a schedule string into (day_spec, hour, minute).

    day_spec is one of: "daily", "weekday", "monday", …, "sunday".
    Returns None if the string is empty or unparsable.
    """
    s = s.strip().lower()
    if not s:
        return None

    parts = s.split()
    if len(parts) == 1:
        # "09:00"
        day_spec = "daily"
        time_str = parts[0]
    elif len(parts) == 2:
        # "weekday 09:00" or "friday 17:00"
        day_spec = parts[0]
        time_str = parts[1]
    else:
        logger.warning("Unparsable schedule %r — ignored", s)
        return None

    try:
        h, m = time_str.split(":")
        return day_spec, int(h), int(m)
    except (ValueError, AttributeError):
        logger.warning("Unparsable schedule time %r — ignored", time_str)
        return None


def _should_fire_now(parsed: tuple[str, int, int], now: datetime) -> bool:
    """Return True if `now` matches the schedule (minute-level precision)."""
    day_spec, hour, minute = parsed
    if now.hour != hour or now.minute != minute:
        return False
    if day_spec == "daily":
        return True
    if day_spec == "weekday":
        return now.weekday() < 5
    if day_spec in _WEEKDAYS:
        return now.weekday() == _WEEKDAYS[day_spec]
    return False


class Scheduler:
    """Background scheduler that auto-generates artifacts on a configured schedule.

    Fires at most once per minute per job. Tracks last-fired times in
    {state_dir}/scheduler_state.json so restarts don't re-fire the same minute.
    """

    def __init__(self, cfg: "Config", daemon: "GhostDaemon") -> None:
        self._cfg = cfg
        self._daemon = daemon
        self._state_path = cfg.state_dir / "scheduler_state.json"
        self._last_fired: dict[str, str] = {}  # job_name → ISO minute string "2026-10-24T09:00"
        self._load_state()

    # ── Persistence ────────────────────────────────────────────────────────────

    def _load_state(self) -> None:
        try:
            if self._state_path.exists():
                self._last_fired = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.debug("Could not load scheduler state: %s", exc)
            self._last_fired = {}

    def _save_state(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps(self._last_fired), encoding="utf-8")
        except Exception as exc:
            logger.debug("Could not save scheduler state: %s", exc)

    # ── Main loop ──────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Long-running background task. Checks every 60 seconds."""
        logger.info("Scheduler started (standup=%r, digest=%r)",
                    self._cfg.schedule.standup, self._cfg.schedule.digest)
        while True:
            await asyncio.sleep(60)
            now = datetime.utcnow().replace(second=0, microsecond=0)
            minute_key = now.strftime("%Y-%m-%dT%H:%M")

            jobs = {
                "standup": self._cfg.schedule.standup,
                "digest": self._cfg.schedule.digest,
            }
            for job_name, schedule_str in jobs.items():
                if not schedule_str:
                    continue
                parsed = _parse_schedule(schedule_str)
                if parsed is None:
                    continue
                if self._last_fired.get(job_name) == minute_key:
                    continue  # already fired this minute
                if _should_fire_now(parsed, now):
                    logger.info("Scheduler firing job %r at %s", job_name, minute_key)
                    try:
                        await self._fire(job_name, now)
                        self._last_fired[job_name] = minute_key
                        self._save_state()
                    except Exception as exc:
                        logger.warning("Scheduled job %r failed: %s", job_name, exc)

    # ── Job dispatch ───────────────────────────────────────────────────────────

    async def _fire(self, job_name: str, now: datetime) -> None:
        if job_name == "standup":
            await self._fire_standup(now)
        elif job_name == "digest":
            await self._fire_digest(now)

    async def _fire_standup(self, now: datetime) -> None:
        from ghost_daemon.agents.standup_agent import StandupAgent
        since = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0)
        agent = StandupAgent(
            self._cfg, self._daemon.llm,
            self._daemon.project_map, self._daemon.tailer,
        )
        draft = await agent.run(since=since)
        draft_id = self._daemon.gate.submit(draft)
        logger.info("Scheduled standup draft created: %s", draft_id)

    async def _fire_digest(self, now: datetime) -> None:
        from ghost_daemon.agents.digest_agent import DigestAgent
        period_end = now
        period_start = now - timedelta(days=7)
        agent = DigestAgent(
            self._cfg, self._daemon.llm,
            self._daemon.project_map, self._daemon.tailer,
            qdrant=self._daemon.qdrant,
        )
        draft = await agent.run(period_start, period_end)
        draft_id = self._daemon.gate.submit(draft)
        logger.info("Scheduled digest draft created: %s", draft_id)
