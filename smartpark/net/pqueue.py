"""Tiny persistent FIFO queue on SQLite - offline resilience.

If the internet drops, Telegram alerts and ThingSpeak readings are stored here
and flushed when connectivity returns, instead of being lost.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path


class PersistentQueue:
    def __init__(self, path: Path, name: str, max_items: int = 5000):
        self.name = name
        self.max_items = max_items
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute(
            f"CREATE TABLE IF NOT EXISTS q_{name} (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "created REAL, expires REAL, payload TEXT)")
        self._db.commit()

    def push(self, payload: dict, ttl_s: float | None = None) -> None:
        now = time.time()
        with self._lock:
            self._db.execute(f"INSERT INTO q_{self.name}(created, expires, payload) VALUES (?,?,?)",
                             (now, now + ttl_s if ttl_s else None, json.dumps(payload)))
            # bound the queue: drop the oldest beyond max_items
            self._db.execute(f"DELETE FROM q_{self.name} WHERE id NOT IN "
                             f"(SELECT id FROM q_{self.name} ORDER BY id DESC LIMIT ?)", (self.max_items,))
            self._db.commit()

    def peek(self, limit: int = 50) -> list[tuple[int, float, dict]]:
        with self._lock:
            self._db.execute(f"DELETE FROM q_{self.name} WHERE expires IS NOT NULL AND expires < ?",
                             (time.time(),))
            rows = self._db.execute(f"SELECT id, created, payload FROM q_{self.name} ORDER BY id LIMIT ?",
                                    (limit,)).fetchall()
        return [(r[0], r[1], json.loads(r[2])) for r in rows]

    def remove(self, ids: list[int]) -> None:
        if not ids:
            return
        with self._lock:
            self._db.executemany(f"DELETE FROM q_{self.name} WHERE id = ?", [(i,) for i in ids])
            self._db.commit()

    def __len__(self) -> int:
        with self._lock:
            return self._db.execute(f"SELECT COUNT(*) FROM q_{self.name}").fetchone()[0]
