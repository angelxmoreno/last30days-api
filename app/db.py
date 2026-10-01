"""SQLite store for jobs. One connection, guarded by a lock (calls are tiny and local)."""

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  seq INTEGER NOT NULL,
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  topic TEXT,
  client_ref TEXT,
  cache_key TEXT NOT NULL,
  cache_hit INTEGER NOT NULL DEFAULT 0,
  request TEXT NOT NULL,
  progress TEXT NOT NULL,
  result TEXT,
  error TEXT,
  raw_path TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  expires_at TEXT
);
CREATE INDEX IF NOT EXISTS jobs_cache ON jobs(cache_key, status, finished_at);
CREATE INDEX IF NOT EXISTS jobs_seq ON jobs(seq);
"""
TERMINAL = ("succeeded", "partial", "failed", "canceled")
JSON_COLS = ("request", "progress", "result", "error")


def now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()

    def _q(self, sql: str, args: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def insert(self, row: dict[str, Any]) -> None:
        row = {**row, **{c: json.dumps(row[c]) for c in JSON_COLS if row.get(c) is not None}}
        cols = ",".join(row)
        marks = ",".join("?" for _ in row)
        with self._lock:
            self._db.execute(
                f"INSERT INTO jobs (seq,{cols}) VALUES ((SELECT COALESCE(MAX(seq),0)+1 FROM jobs),{marks})",
                tuple(row.values()),
            )

    def update(self, job_id: str, **fields: Any) -> None:
        fields = {k: (json.dumps(v) if k in JSON_COLS and v is not None else v) for k, v in fields.items()}
        sets = ",".join(f"{k}=?" for k in fields)
        with self._lock:
            self._db.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*fields.values(), job_id))

    def get(self, job_id: str) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM jobs WHERE id=?", (job_id,))
        return _decode(rows[0]) if rows else None

    def find_cached(self, cache_key: str, min_finished: str) -> dict[str, Any] | None:
        rows = self._q(
            "SELECT * FROM jobs WHERE cache_key=? AND status IN ('succeeded','partial') "
            "AND finished_at>=? AND cache_hit=0 ORDER BY finished_at DESC LIMIT 1",
            (cache_key, min_finished),
        )
        return _decode(rows[0]) if rows else None

    def find_active(self, cache_key: str) -> dict[str, Any] | None:
        rows = self._q(
            "SELECT * FROM jobs WHERE cache_key=? AND status IN ('queued','running') LIMIT 1",
            (cache_key,),
        )
        return _decode(rows[0]) if rows else None

    def count(self, status: str) -> int:
        return int(self._q("SELECT COUNT(*) c FROM jobs WHERE status=?", (status,))[0]["c"])

    def by_status(self, status: str) -> list[dict[str, Any]]:
        return [_decode(r) for r in self._q("SELECT * FROM jobs WHERE status=? ORDER BY seq", (status,))]

    def list_jobs(
        self,
        *,
        status: str | None,
        kind: str | None,
        topic: str | None,
        client_ref: str | None,
        before_seq: int | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        where: list[str] = ["1=1"]
        args: list[Any] = []
        for col, val in (("status", status), ("kind", kind), ("client_ref", client_ref)):
            if val:
                where.append(f"{col}=?")
                args.append(val)
        if topic:
            where.append("topic LIKE ? ESCAPE '\\'")
            esc = topic.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            args.append(f"%{esc}%")
        if before_seq is not None:
            where.append("seq<?")
            args.append(before_seq)
        rows = self._q(
            f"SELECT * FROM jobs WHERE {' AND '.join(where)} ORDER BY seq DESC LIMIT ?",
            (*args, limit),
        )
        return [_decode(r) for r in rows]

    def purge_expired(self) -> list[str | None]:
        """Delete expired terminal jobs. Returns their raw_paths so callers can delete files."""
        cutoff = now()
        rows = self._q(
            "SELECT id, raw_path FROM jobs WHERE expires_at IS NOT NULL AND expires_at<? "
            "AND status IN ('succeeded','partial','failed','canceled')",
            (cutoff,),
        )
        with self._lock:
            self._db.executemany("DELETE FROM jobs WHERE id=?", [(r["id"],) for r in rows])
        return [r["raw_path"] for r in rows]

    def raw_path_in_use(self, raw_path: str) -> bool:
        return bool(self._q("SELECT 1 FROM jobs WHERE raw_path=? LIMIT 1", (raw_path,)))


def _decode(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for c in JSON_COLS:
        if d.get(c) is not None:
            d[c] = json.loads(d[c])
    d["cache_hit"] = bool(d["cache_hit"])
    return d


def expiry(days: int) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()
