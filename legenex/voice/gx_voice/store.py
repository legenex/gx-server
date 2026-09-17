"""Durable node-2 records (SQLite, WAL, one connection per call).

* ``jobs``: the queue and every job's request, state, takes and timings.
* ``refs``: reference clips (content-addressed ``ref-<sha256[:32]>``).
* ``voices``: the replica of saved voices that the OpenAI speech endpoint
  resolves by id or name. gx10-01 owns the voices; it pushes every change.
* ``events``: a bounded lifecycle log.

Audio lives on disk under the data root; the database only stores ids and
metadata, and nothing here is ever returned as a filesystem path.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from collections.abc import Iterator

QUEUED = "queued"
WAITING = "waiting_for_resource"
LOADING = "loading_model"
GENERATING = "generating"
PROCESSING = "processing"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
ACTIVE = (QUEUED, WAITING, LOADING, GENERATING, PROCESSING)
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
        notes_json TEXT NOT NULL DEFAULT '[]',
        error_code TEXT,
        error_message TEXT,
        retryable INTEGER NOT NULL DEFAULT 0,
        client_ref TEXT,
        cancel_requested INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created_at)",
    """
    CREATE TABLE IF NOT EXISTS refs (
        id TEXT PRIMARY KEY,
        created_at REAL NOT NULL,
        filename TEXT NOT NULL,
        container TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        sha256 TEXT NOT NULL,
        duration_s REAL NOT NULL,
        last_used REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS voices (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        name_key TEXT NOT NULL,
        spec_json TEXT NOT NULL,
        instructions TEXT NOT NULL DEFAULT '',
        language TEXT NOT NULL DEFAULT 'auto',
        version INTEGER NOT NULL DEFAULT 1,
        updated_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS voices_name ON voices(name_key)",
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


def new_job_id() -> str:
    return f"vox-{uuid.uuid4().hex}"


def name_key(name: str) -> str:
    return " ".join(name.lower().split())


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
    def create_job(self, *, operation: str, title: str, request: dict, client_ref: str | None) -> str:
        job_id = new_job_id()
        now = time.time()
        with self._write_lock, self._conn() as db:
            db.execute("INSERT INTO jobs(id, operation, status, created_at, updated_at, title, request_json, "
                       "client_ref) VALUES(?,?,?,?,?,?,?,?)",
                       (job_id, operation, QUEUED, now, now, title, json.dumps(request), client_ref))
        return job_id

    def update_job(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        cols, vals = [], []
        for k, v in fields.items():
            if k in {"result", "timings", "notes"}:
                k, v = f"{k}_json", json.dumps(v)
            cols.append(f"{k}=?")
            vals.append(v)
        cols.append("updated_at=?")
        vals += [time.time(), job_id]
        with self._write_lock, self._conn() as db:
            db.execute(f"UPDATE jobs SET {', '.join(cols)} WHERE id=?", vals)  # noqa: S608 - fixed names

    def transition(self, job_id: str, status: str, detail: str = "", progress: float | None = None) -> None:
        extra: dict[str, Any] = {"status": status, "detail": detail, "progress": progress}
        if status in TERMINAL:
            extra["finished_at"] = time.time()
        self.update_job(job_id, **extra)

    def get_job(self, job_id: str) -> dict | None:
        with self._conn() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _job_row(row) if row else None

    def list_jobs(self, *, status: str | None = None, limit: int = 50) -> list[dict]:
        q, args = "SELECT * FROM jobs", []
        if status == "active":
            q += f" WHERE status IN ({','.join('?' * len(ACTIVE))})"
            args += list(ACTIVE)
        elif status:
            q += " WHERE status=?"
            args.append(status)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self._conn() as db:
            return [_job_row(r) for r in db.execute(q, args).fetchall()]

    def next_queued(self) -> dict | None:
        with self._conn() as db:
            row = db.execute("SELECT * FROM jobs WHERE status IN (?, ?) AND cancel_requested=0 "
                             "ORDER BY created_at LIMIT 1", (QUEUED, WAITING)).fetchone()
        return _job_row(row) if row else None

    def count_active(self) -> int:
        with self._conn() as db:
            marks = ",".join("?" * len(ACTIVE))
            return db.execute(f"SELECT COUNT(*) FROM jobs WHERE status IN ({marks})",  # noqa: S608 - placeholders
                              ACTIVE).fetchone()[0]

    def count_status(self, status: str) -> int:
        with self._conn() as db:
            return db.execute("SELECT COUNT(*) FROM jobs WHERE status=?", (status,)).fetchone()[0]

    def recover_interrupted(self) -> int:
        """Jobs mid-flight when the supervisor died cannot resume: fail them honestly."""
        now = time.time()
        with self._write_lock, self._conn() as db:
            cur = db.execute(
                "UPDATE jobs SET status=?, detail='', error_code='interrupted', "
                "error_message='The voice service restarted while this job ran. Retry it.', "
                "retryable=1, finished_at=?, updated_at=? WHERE status IN (?,?,?)",
                (FAILED, now, now, LOADING, GENERATING, PROCESSING))
            return cur.rowcount

    def delete_job(self, job_id: str) -> None:
        with self._write_lock, self._conn() as db:
            db.execute("DELETE FROM jobs WHERE id=?", (job_id,))

    def stats(self) -> dict:
        with self._conn() as db:
            rows = db.execute("SELECT status, COUNT(*) c FROM jobs GROUP BY status").fetchall()
            last = db.execute("SELECT id, finished_at, timings_json FROM jobs WHERE status=? "
                              "ORDER BY finished_at DESC LIMIT 1", (COMPLETED,)).fetchone()
        return {"by_status": {r["status"]: r["c"] for r in rows},
                "last_success": ({"job_id": last["id"], "finished_at": last["finished_at"],
                                  "timings": json.loads(last["timings_json"])} if last else None)}

    # ------------------------------------------------------------ refs --
    def add_ref(self, **row: Any) -> None:
        with self._write_lock, self._conn() as db:
            db.execute("INSERT OR IGNORE INTO refs(id, created_at, filename, container, size_bytes, sha256, "
                       "duration_s) VALUES(:id, :created_at, :filename, :container, :size_bytes, :sha256, "
                       ":duration_s)", row)

    def get_ref(self, ref_id: str) -> dict | None:
        with self._conn() as db:
            row = db.execute("SELECT * FROM refs WHERE id=?", (ref_id,)).fetchone()
        return dict(row) if row else None

    def touch_ref(self, ref_id: str) -> None:
        with self._write_lock, self._conn() as db:
            db.execute("UPDATE refs SET last_used=? WHERE id=?", (time.time(), ref_id))

    def delete_ref(self, ref_id: str) -> None:
        with self._write_lock, self._conn() as db:
            db.execute("DELETE FROM refs WHERE id=?", (ref_id,))

    def refs_in_use(self, ref_id: str) -> list[str]:
        with self._conn() as db:
            rows = db.execute("SELECT id, spec_json FROM voices").fetchall()
        return [r["id"] for r in rows if json.loads(r["spec_json"]).get("reference_id") == ref_id]

    # ---------------------------------------------------------- voices --
    def put_voice(self, rec: dict) -> dict:
        with self._write_lock, self._conn() as db:
            db.execute(
                "INSERT INTO voices(id, name, name_key, spec_json, instructions, language, version, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
                "name_key=excluded.name_key, spec_json=excluded.spec_json, instructions=excluded.instructions, "
                "language=excluded.language, version=excluded.version, updated_at=excluded.updated_at",
                (rec["id"], rec["name"], name_key(rec["name"]), json.dumps(rec["spec"]), rec["instructions"],
                 rec["language"], rec["version"], time.time()))
        return self.get_voice(rec["id"]) or rec

    def get_voice(self, voice_id: str) -> dict | None:
        with self._conn() as db:
            row = db.execute("SELECT * FROM voices WHERE id=?", (voice_id,)).fetchone()
        return _voice_row(row) if row else None

    def voices_named(self, name: str) -> list[dict]:
        with self._conn() as db:
            rows = db.execute("SELECT * FROM voices WHERE name_key=?", (name_key(name),)).fetchall()
        return [_voice_row(r) for r in rows]

    def list_voices(self) -> list[dict]:
        with self._conn() as db:
            rows = db.execute("SELECT * FROM voices ORDER BY name_key").fetchall()
        return [_voice_row(r) for r in rows]

    def delete_voice(self, voice_id: str) -> bool:
        with self._write_lock, self._conn() as db:
            return db.execute("DELETE FROM voices WHERE id=?", (voice_id,)).rowcount > 0

    # ---------------------------------------------------------- events --
    def event(self, kind: str, **data: Any) -> None:
        with self._write_lock, self._conn() as db:
            db.execute("INSERT INTO events(at, kind, data_json) VALUES(?,?,?)",
                       (time.time(), kind, json.dumps(data, default=str)))
            db.execute("DELETE FROM events WHERE id < (SELECT MAX(id) - 5000 FROM events)")

    def events(self, limit: int = 100) -> list[dict]:
        with self._conn() as db:
            rows = db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"at": r["at"], "kind": r["kind"], **json.loads(r["data_json"])} for r in rows]


def _job_row(row: sqlite3.Row) -> dict:
    d = dict(row)
    for k in ("request_json", "result_json", "timings_json", "notes_json"):
        raw = d.pop(k)
        d[k[:-5]] = json.loads(raw) if raw else None
    d["cancel_requested"] = bool(d["cancel_requested"])
    d["retryable"] = bool(d["retryable"])
    return d


def _voice_row(row: sqlite3.Row) -> dict:
    d = dict(row)
    d.pop("name_key", None)
    d["spec"] = json.loads(d.pop("spec_json"))
    return d
