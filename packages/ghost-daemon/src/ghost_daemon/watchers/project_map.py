from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from ghost_daemon.config import Config

logger = logging.getLogger(__name__)

_STATE_FILE = "project_map.json"


class ProjectMap:
    """Deterministic repo-path → session-JSONL mapper.

    Resolution order:
    1. In-memory cache
    2. Walk session dirs, parse cwd from JSONL first line
    3. Slug-convention fallback (same algorithm Claude Code uses)
    """

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._state_file = cfg.state_dir / _STATE_FILE
        self._cache: dict[str, Path] = {}
        self._load_cache()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve(self, repo_path: str | Path) -> Path | None:
        key = _normalise(str(repo_path))
        if key in self._cache and self._cache[key].exists():
            return self._cache[key]
        self.refresh()
        return self._cache.get(key)

    def refresh(self) -> None:
        found: dict[str, Path] = {}
        for session_dir in self._cfg.resolved_session_dirs:
            for jsonl in session_dir.rglob("*.jsonl"):
                cwd = _read_cwd(jsonl)
                if cwd:
                    found[_normalise(cwd)] = jsonl
        # Slug-convention fallback for directories we couldn't read a cwd from
        for session_dir in self._cfg.resolved_session_dirs:
            for project_dir in (d for d in session_dir.iterdir() if d.is_dir()):
                slug = project_dir.name
                jsonls = sorted(project_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
                if jsonls and slug not in found:
                    found[slug] = jsonls[0]
        self._cache.update(found)
        self._save_cache()

    def register(self, repo_path: str | Path, jsonl_path: Path) -> None:
        self._cache[_normalise(str(repo_path))] = jsonl_path
        self._save_cache()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_cache(self) -> None:
        if not self._state_file.exists():
            return
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            self._cache = {k: Path(v) for k, v in data.items()}
        except Exception as exc:
            logger.warning("Could not load project_map cache: %s", exc)

    def _save_cache(self) -> None:
        self._cfg.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._state_file.write_text(
                json.dumps({k: str(v) for k, v in self._cache.items()}, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("Could not save project_map cache: %s", exc)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _normalise(path: str) -> str:
    """Normalise a path to the slug Claude Code would produce."""
    return re.sub(r"[:/\\]", "-", path).lower()


def _read_cwd(jsonl_path: Path) -> str | None:
    try:
        with open(jsonl_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    break
                cwd = (
                    data.get("cwd")
                    or data.get("workingDirectory")
                    or data.get("project_root")
                    or (data.get("message") or {}).get("cwd")
                )
                if cwd:
                    return str(cwd)
                break
    except OSError:
        pass
    return None
