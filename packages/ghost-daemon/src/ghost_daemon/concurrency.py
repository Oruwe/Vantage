import asyncio
from pathlib import Path


class LockRegistry:
    """Per-path and per-DB asyncio locks.

    All session JSONL reads and SQLite writes acquire the matching lock so that
    commit-driven summarisation and live probes never race on the same file.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def get(self, key: Path | str) -> asyncio.Lock:
        k = str(key)
        if k not in self._locks:
            self._locks[k] = asyncio.Lock()
        return self._locks[k]


_registry = LockRegistry()


def get_lock(key: Path | str) -> asyncio.Lock:
    return _registry.get(key)
