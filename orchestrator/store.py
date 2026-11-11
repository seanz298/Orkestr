from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from .contract import IN_PROGRESS, INTERRUPTED, QUEUED, TaskSpec


class Store:
    """SQLite task + event log. Shared by the worker and the CLI."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks(
              id TEXT PRIMARY KEY, spec TEXT NOT NULL, state TEXT NOT NULL,
              note TEXT DEFAULT '', cost_usd REAL DEFAULT 0,
              created REAL, updated REAL);
            CREATE TABLE IF NOT EXISTS events(
              n INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, ts REAL,
              kind TEXT, payload TEXT);
            """
        )

    def add_task(self, spec: TaskSpec) -> str:
        spec.id = spec.id or time.strftime("%m%d") + "-" + uuid.uuid4().hex[:6]
        now = time.time()
        self.db.execute(
            "INSERT INTO tasks(id,spec,state,created,updated) VALUES(?,?,?,?,?)",
            (spec.id, spec.model_dump_json(), QUEUED, now, now),
        )
        self.event(spec.id, "submitted", {"title": spec.title})
        return spec.id

    def get(self, task_id: str) -> Optional[sqlite3.Row]:
        return self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()

    def spec(self, task_id: str) -> TaskSpec:
        row = self.get(task_id)
        if row is None:
            raise KeyError(f"no such task: {task_id}")
        return TaskSpec.model_validate_json(row["spec"])

    def set_state(self, task_id: str, state: str, note: str = "") -> None:
        self.db.execute(
            "UPDATE tasks SET state=?, note=?, updated=? WHERE id=?",
            (state, note, time.time(), task_id),
        )
        self.event(task_id, "state", {"state": state, "note": note})

    def add_cost(self, task_id: str, usd: float) -> None:
        self.db.execute(
            "UPDATE tasks SET cost_usd=cost_usd+?, updated=? WHERE id=?",
            (usd, time.time(), task_id),
        )

    def event(self, task_id: str, kind: str, payload: Dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO events(task_id,ts,kind,payload) VALUES(?,?,?,?)",
            (task_id, time.time(), kind, json.dumps(payload, ensure_ascii=False)),
        )

    def events(self, task_id: str) -> List[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM events WHERE task_id=? ORDER BY n", (task_id,)
        ).fetchall()

    def list(self) -> List[sqlite3.Row]:
        return self.db.execute("SELECT * FROM tasks ORDER BY created").fetchall()

    def next_queued(self) -> Optional[str]:
        row = self.db.execute(
            "SELECT id FROM tasks WHERE state=? ORDER BY created LIMIT 1", (QUEUED,)
        ).fetchone()
        return row["id"] if row else None

    def mark_interrupted(self) -> int:
        """Called at worker start: anything mid-flight died with the old worker."""
        n = 0
        for row in self.list():
            if row["state"] in IN_PROGRESS:
                self.set_state(row["id"], INTERRUPTED, "worker restarted; use `retry`")
                n += 1
        return n
