"""Durable job and upload records (SQLite, WAL, one connection per call).

Binaries live on disk under ``<data_root>/jobs/<job_id>/`` and
``<data_root>/uploads/``; the database only ever stores ids and metadata.
Browser-facing layers never receive a filesystem path from here.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

# Status vocabulary shared with GX-Playground.
QUEUED = "queued"
WAITING = "waiting_for_resource"
LOADING = "loading_model"
PREPARING = "preparing"
GENERATING = "generating"
PROCESSING = "processing"
SAVING = "saving"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"

ACTIVE = (QUEUED, WAITING, LOADING, PREPARING, GENERATING, PROCESSING, SAVING)
TERMINAL = (COMPLETED, FAILED, CANCELLED)

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        operation TEXT NOT NULL,
        status TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '',
        progress REAL,
        created_at REAL NOT NULL,
        started_at REAL,
        finished_at REAL,
        updated_at REAL NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        request_json TEXT NOT NULL,
        result_json TEXT,
        timings_json TEXT NOT NULL DEFAULT '{}',
        model_json TEXT NOT NULL,
        error_code TEXT,
        error_message TEXT,
        retryable INTEGER NOT NULL DEFAULT 0,
        parent_job_id TEXT,
        parent_index INTEGER,
        source_json TEXT,
        engine_task_id TEXT,
        cancel_requested INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created_at)",
    "CREATE INDEX IF NOT EXISTS jobs_parent ON jobs(parent_job_id)",
    """
    CREATE TABLE IF NOT EXISTS uploads (
        id TEXT PRIMARY KEY,
        created_at REAL NOT NULL,
        filename TEXT NOT NULL,
        container TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        sha256 TEXT NOT NULL,
        duration_s REAL,
        sample_rate INTEGER,
        channels INTEGER,
        stored_name TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at REAL NOT NULL,
        kind TEXT NOT NULL,
        data_json TEXT NOT NULL
    )
    """,
    "CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)",
]
SCHEMA_VERSION = "1"


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        with self._conn() as db:
            db.execute("PRAGMA journal_mode=WAL")
            for stmt in SCHEMA:
                db.execute(stmt)
            db.execute("INSERT OR IGNORE INTO meta(k, v) VALUES('schema_version', ?)", (SCHEMA_VERSION,))

    @contextlib.contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA busy_timeout=30000")
            yield db
        finally:
            db.close()

    # ------------------------------------------------------------ jobs --
    def create_job(self, *, operation: str, title: str, request: dict, model: dict,
                   parent_job_id: str | None, parent_index: int | None, source: dict | None,
                   status: str = QUEUED, detail: str = "") -> str:
        job_id = new_id("mus")
        now = time.time()
        with self._write_lock, self._conn() as db:
            db.execute(
                "INSERT INTO jobs(id, operation, status, detail, created_at, updated_at, title, request_json, "
                "model_json, parent_job_id, parent_index, source_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, operation, status, detail, now, now, title, json.dumps(request), json.dumps(model),
                 parent_job_id, parent_index, json.dumps(source) if source else None),
            )
        return job_id

    def update_job(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        cols, vals = [], []
        for k, v in fields.items():
            if k in {"result", "timings", "request"}:
                k, v = f"{k}_json", json.dumps(v)
            cols.append(f"{k}=?")
            vals.append(v)
        cols.append("updated_at=?")
        vals.append(time.time())
        vals.append(job_id)
        with self._write_lock, self._conn() as db:
            db.execute(f"UPDATE jobs SET {', '.join(cols)} WHERE id=?", vals)

    def transition(self, job_id: str, status: str, detail: str = "", progress: float | None = None) -> None:
        extra: dict[str, Any] = {"status": status, "detail": detail, "progress": progress}
        if status in TERMINAL:
            extra["finished_at"] = time.time()
        self.update_job(job_id, **extra)

    def get_job(self, job_id: str) -> dict | None:
        with self._conn() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _job_row(row) if row else None

    def list_jobs(self, *, status: str | None = None, limit: int = 50, before: float | None = None,
                  operation: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM jobs", []
        where = []
        if operation == "creative":
            where.append("operation<>'analyze'")
        elif operation:
            where.append("operation=?")
            args.append(operation)
        if status == "active":
            where.append(f"status IN ({','.join('?' * len(ACTIVE))})")
            args += list(ACTIVE)
        elif status:
            where.append("status=?")
            args.append(status)
        if before:
            where.append("created_at<?")
            args.append(before)
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self._conn() as db:
            return [_job_row(r) for r in db.execute(q, args).fetchall()]

    def children(self, job_id: str) -> list[dict]:
        with self._conn() as db:
            rows = db.execute("SELECT * FROM jobs WHERE parent_job_id=? ORDER BY created_at", (job_id,)).fetchall()
        return [_job_row(r) for r in rows]

    def next_queued(self) -> dict | None:
        with self._conn() as db:
            row = db.execute(
                "SELECT * FROM jobs WHERE status IN (?, ?) AND cancel_requested=0 ORDER BY created_at LIMIT 1",
                (QUEUED, WAITING),
            ).fetchone()
        return _job_row(row) if row else None

    def count_active(self) -> int:
        with self._conn() as db:
            return db.execute(
                f"SELECT COUNT(*) FROM jobs WHERE status IN ({','.join('?' * len(ACTIVE))})", ACTIVE
            ).fetchone()[0]

    def recover_interrupted(self) -> int:
        """Jobs that were mid-flight when the supervisor died cannot resume
        (the engine's in-memory queue is gone). Fail them honestly."""
        now = time.time()
        with self._write_lock, self._conn() as db:
            cur = db.execute(
                "UPDATE jobs SET status=?, detail='', error_code='interrupted', "
                "error_message='The music service restarted while this track was being made. Retry it.', "
                "retryable=1, finished_at=?, updated_at=? WHERE status IN (?,?,?,?,?)",
                (FAILED, now, now, LOADING, PREPARING, GENERATING, PROCESSING, SAVING),
            )
            return cur.rowcount

    def stats(self) -> dict:
        with self._conn() as db:
            rows = db.execute("SELECT status, COUNT(*) c FROM jobs GROUP BY status").fetchall()
            last = db.execute(
                "SELECT id, finished_at, timings_json FROM jobs WHERE status=? ORDER BY finished_at DESC LIMIT 1",
                (COMPLETED,),
            ).fetchone()
        return {
            "by_status": {r["status"]: r["c"] for r in rows},
            "last_success": ({"job_id": last["id"], "finished_at": last["finished_at"],
                              "timings": json.loads(last["timings_json"])} if last else None),
        }

    def delete_job(self, job_id: str) -> None:
        with self._write_lock, self._conn() as db:
            db.execute("UPDATE jobs SET parent_job_id=NULL WHERE parent_job_id=?", (job_id,))
            db.execute("DELETE FROM jobs WHERE id=?", (job_id,))

    # --------------------------------------------------------- uploads --
    def add_upload(self, **row: Any) -> None:
        cols = ",".join(row)
        with self._write_lock, self._conn() as db:
            db.execute(f"INSERT INTO uploads({cols}) VALUES({','.join('?' * len(row))})", tuple(row.values()))

    def get_upload(self, upload_id: str) -> dict | None:
        with self._conn() as db:
            row = db.execute("SELECT * FROM uploads WHERE id=?", (upload_id,)).fetchone()
        return dict(row) if row else None

    # ---------------------------------------------------------- events --
    def event(self, kind: str, **data: Any) -> None:
        with self._write_lock, self._conn() as db:
            db.execute("INSERT INTO events(at, kind, data_json) VALUES(?,?,?)", (time.time(), kind, json.dumps(data)))
            db.execute("DELETE FROM events WHERE id < (SELECT MAX(id) - 5000 FROM events)")

    def events(self, limit: int = 100) -> list[dict]:
        with self._conn() as db:
            rows = db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"at": r["at"], "kind": r["kind"], **json.loads(r["data_json"])} for r in rows]


def _job_row(row: sqlite3.Row) -> dict:
    d = dict(row)
    for k in ("request_json", "result_json", "timings_json", "model_json", "source_json"):
        raw = d.pop(k)
        d[k[:-5]] = json.loads(raw) if raw else None
    d["cancel_requested"] = bool(d["cancel_requested"])
    d["retryable"] = bool(d["retryable"])
    return d
