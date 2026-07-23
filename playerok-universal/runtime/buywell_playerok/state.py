from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
RETENTION_SECONDS = 90 * 24 * 60 * 60


class RuntimeState:
    def __init__(self, path: Path):
        self.path = path
        self._init_lock = threading.Lock()
        self._initialized = False

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        database = sqlite3.connect(self.path, timeout=15)
        database.execute("PRAGMA journal_mode=WAL")
        database.execute("PRAGMA busy_timeout=15000")
        database.execute("PRAGMA foreign_keys=ON")
        if not self._initialized:
            self._initialize(database)
        return database

    def _initialize(self, database: sqlite3.Connection) -> None:
        with self._init_lock:
            if self._initialized:
                return
            database.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS seen_events (
                    event_id TEXT PRIMARY KEY,
                    observed_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    body TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at REAL NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS actions (
                    idempotency_key TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    result TEXT,
                    updated_at REAL NOT NULL
                );
                """
            )
            database.execute(
                "INSERT INTO settings(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
            database.commit()
            self._initialized = True

    def get_setting(self, key: str) -> str | None:
        with self.connect() as database:
            row = database.execute(
                "SELECT value FROM settings WHERE key=?", (key,)
            ).fetchone()
        return str(row[0]) if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as database:
            database.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def enqueue_once(self, event_id: str, body: dict[str, Any]) -> bool:
        now = time.time()
        serialized = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        with self.connect() as database:
            inserted = database.execute(
                "INSERT OR IGNORE INTO seen_events(event_id,observed_at) VALUES(?,?)",
                (event_id, now),
            ).rowcount
            if inserted:
                database.execute(
                    "INSERT INTO outbox(event_id,body,available_at,created_at) "
                    "VALUES(?,?,?,?)",
                    (event_id, serialized, now, now),
                )
        return inserted == 1

    def outbox(self, limit: int = 100) -> list[tuple[int, dict[str, Any]]]:
        with self.connect() as database:
            rows = database.execute(
                "SELECT id,body FROM outbox WHERE available_at<=? ORDER BY id LIMIT ?",
                (time.time(), limit),
            ).fetchall()
        return [(int(row[0]), json.loads(row[1])) for row in rows]

    def accept_events(self, row_ids: Iterable[int]) -> None:
        values = [(int(row_id),) for row_id in row_ids]
        if not values:
            return
        with self.connect() as database:
            database.executemany("DELETE FROM outbox WHERE id=?", values)

    def retry_events(self, row_ids: Iterable[int]) -> None:
        values = [int(row_id) for row_id in row_ids]
        if not values:
            return
        with self.connect() as database:
            for row_id in values:
                row = database.execute(
                    "SELECT attempts FROM outbox WHERE id=?", (row_id,)
                ).fetchone()
                attempts = int(row[0]) + 1 if row else 1
                delay = min(300, 5 * (2 ** min(attempts, 6)))
                database.execute(
                    "UPDATE outbox SET attempts=?,available_at=? WHERE id=?",
                    (attempts, time.time() + delay, row_id),
                )

    def action(self, key: str) -> tuple[str, dict[str, Any] | None] | None:
        with self.connect() as database:
            row = database.execute(
                "SELECT state,result FROM actions WHERE idempotency_key=?", (key,)
            ).fetchone()
        if not row:
            return None
        return str(row[0]), json.loads(row[1]) if row[1] else None

    def begin_action(self, key: str) -> bool:
        with self.connect() as database:
            return (
                database.execute(
                    "INSERT OR IGNORE INTO actions(idempotency_key,state,result,updated_at) "
                    "VALUES(?,'pending',NULL,?)",
                    (key, time.time()),
                ).rowcount
                == 1
            )

    def finish_action(self, key: str, result: dict[str, Any]) -> None:
        with self.connect() as database:
            database.execute(
                "UPDATE actions SET state='terminal',result=?,updated_at=? "
                "WHERE idempotency_key=?",
                (
                    json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                    time.time(),
                    key,
                ),
            )

    def capture_spec(self) -> dict[str, Any]:
        raw = self.get_setting("capture_spec")
        if not raw:
            return {"revision": 0, "digest": "", "subscriptions": []}
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return {"revision": 0, "digest": "", "subscriptions": []}
        return value if isinstance(value, dict) else {
            "revision": 0,
            "digest": "",
            "subscriptions": [],
        }

    def save_capture_spec(self, specification: dict[str, Any]) -> None:
        self.set_setting(
            "capture_spec",
            json.dumps(specification, ensure_ascii=False, separators=(",", ":")),
        )

    def prune(self) -> None:
        cutoff = time.time() - RETENTION_SECONDS
        with self.connect() as database:
            database.execute(
                "DELETE FROM seen_events WHERE observed_at<? "
                "AND event_id NOT IN (SELECT event_id FROM outbox)",
                (cutoff,),
            )
            database.execute(
                "DELETE FROM actions WHERE state='terminal' AND updated_at<?",
                (cutoff,),
            )
