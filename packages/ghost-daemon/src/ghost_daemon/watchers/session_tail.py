from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from vantage.schemas import Turn
from ghost_daemon.concurrency import get_lock

logger = logging.getLogger(__name__)

_MAX_EXCERPT = 800
_ROLES = frozenset({"user", "assistant", "tool_use", "tool_result", "system"})


@dataclass
class FileState:
    path: Path
    inode: int
    size: int
    mtime: float
    offset: int


class SessionTailer:
    """Reads new turns from Claude Code / Codex session JSONL files.

    Maintains per-file state (inode, size, mtime, offset) and detects
    truncation / log rotation by comparing current stat against stored state.
    On truncation, offset resets to 0 so the new content is re-read cleanly.
    """

    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir
        self._states: dict[Path, FileState] = {}
        self._state_file = state_dir / "offsets.json"
        self._load_states()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def read_new_turns(self, jsonl_path: Path) -> list[Turn]:
        lock = get_lock(jsonl_path)
        async with lock:
            return self._read_new_turns_sync(jsonl_path)

    async def tail_recent(self, jsonl_path: Path, n: int = 40) -> list[Turn]:
        """Return up to n most-recent turns without advancing the offset."""
        lock = get_lock(jsonl_path)
        async with lock:
            return self._tail_recent_sync(jsonl_path, n)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _stat(self, path: Path) -> tuple[int, int, float]:
        """Return (inode, size, mtime). On Windows, inode may be 0 for some FS."""
        s = path.stat()
        inode = s.st_ino
        if inode == 0:
            # Fallback for Windows FS without inodes: use creation time hash
            inode = int(s.st_ctime * 1_000_000) & 0xFFFFFFFF
        return inode, s.st_size, s.st_mtime

    def _is_reset(self, path: Path, stored: FileState) -> bool:
        try:
            inode, size, mtime = self._stat(path)
        except OSError:
            return False
        if inode != stored.inode:
            logger.info("Session file rotated (new inode): %s", path.name)
            return True
        if size < stored.offset:
            logger.info("Session file truncated: %s (was %d, now %d)", path.name, stored.offset, size)
            return True
        return False

    def _ensure_state(self, path: Path) -> FileState:
        if path not in self._states or self._is_reset(path, self._states[path]):
            try:
                inode, size, mtime = self._stat(path)
            except OSError:
                inode, size, mtime = 0, 0, 0.0
            self._states[path] = FileState(path=path, inode=inode, size=size, mtime=mtime, offset=0)
        return self._states[path]

    def _read_new_turns_sync(self, jsonl_path: Path) -> list[Turn]:
        if not jsonl_path.exists():
            return []
        state = self._ensure_state(jsonl_path)
        turns: list[Turn] = []
        try:
            with open(jsonl_path, encoding="utf-8", errors="replace") as f:
                f.seek(state.offset)
                for line in f:
                    turn = _parse_turn(line)
                    if turn:
                        turns.append(turn)
                new_offset = f.tell()
            state.offset = new_offset
            inode, size, mtime = self._stat(jsonl_path)
            state.size = size
            state.mtime = mtime
            self._save_states()
        except OSError as exc:
            logger.warning("Could not read session file %s: %s", jsonl_path.name, exc)
        return turns

    def _tail_recent_sync(self, jsonl_path: Path, n: int) -> list[Turn]:
        if not jsonl_path.exists():
            return []
        turns: list[Turn] = []
        try:
            with open(jsonl_path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    turn = _parse_turn(line)
                    if turn:
                        turns.append(turn)
        except OSError as exc:
            logger.warning("Could not tail session file %s: %s", jsonl_path.name, exc)
        return turns[-n:]

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_states(self) -> None:
        if not self._state_file.exists():
            return
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            for entry in data:
                p = Path(entry["path"])
                self._states[p] = FileState(
                    path=p,
                    inode=entry["inode"],
                    size=entry["size"],
                    mtime=entry["mtime"],
                    offset=entry["offset"],
                )
        except Exception as exc:
            logger.warning("Could not load offset state: %s", exc)

    def _save_states(self) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        try:
            data = [
                {
                    "path": str(s.path),
                    "inode": s.inode,
                    "size": s.size,
                    "mtime": s.mtime,
                    "offset": s.offset,
                }
                for s in self._states.values()
            ]
            self._state_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("Could not save offset state: %s", exc)


# ------------------------------------------------------------------
# JSONL turn parser
# ------------------------------------------------------------------

def _parse_turn(line: str) -> Turn | None:
    line = line.strip()
    if not line:
        return None
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None

    role = data.get("role") or data.get("type") or ""
    if role not in _ROLES:
        return None

    content = data.get("content") or (data.get("message") or {}).get("content") or ""
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                btype = block.get("type", "")
                if btype == "text":
                    parts.append(block.get("text", ""))
                elif btype == "tool_use":
                    parts.append(f"[tool: {block.get('name', '')}]")
            elif isinstance(block, str):
                parts.append(block)
        content = " ".join(parts)

    if not isinstance(content, str) or not content.strip():
        return None

    tool_name: str | None = None
    if role == "tool_use":
        tool_name = data.get("name") or data.get("tool_name")

    ts: datetime | None = None
    ts_raw = data.get("timestamp") or data.get("ts") or data.get("created_at")
    if isinstance(ts_raw, str):
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except ValueError:
            pass

    return Turn(
        role=role,
        content_excerpt=content[:_MAX_EXCERPT],
        tool_name=tool_name,
        timestamp=ts,
    )
