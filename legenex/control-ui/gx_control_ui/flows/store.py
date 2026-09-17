"""Persistence for Creative Flows (tables from migrations/030_flows.sql).

Every multi-row write is one transaction (``BEGIN IMMEDIATE``). Flow updates
use optimistic concurrency: the caller sends the version it edited and a
stale version is refused with 409 instead of overwriting someone's work.
Every saved version is kept (``flow_versions``) so an earlier graph can be
restored. Nothing here deletes Library assets.
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from typing import Any

FLOW_ID = re.compile(r"^flow_[0-9a-f]{24}$")
RUN_ID = re.compile(r"^frun_[0-9a-f]{24}$")
TEMPLATE_ID = re.compile(r"^(tpl_[0-9a-f]{24}|builtin_[a-z0-9_]{2,40})$")
MAX_VERSIONS_KEPT = 200
MAX_LOG_LINES = 200


class FlowError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "flow_error",
                 issues: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.issues = issues or []


def new_flow_id() -> str:
    return "flow_" + secrets.token_hex(12)


def new_run_id() -> str:
    return "frun_" + secrets.token_hex(12)


def new_template_id() -> str:
    return "tpl_" + secrets.token_hex(12)


def _loads(text: str | None, default: Any) -> Any:
    try:
        value = json.loads(text) if text else default
    except ValueError:
        return default
    return default if value is None else value


def _dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)


Connector = Callable[[], AbstractContextManager[sqlite3.Connection]]


class FlowStore:
    def __init__(self, connect: Connector) -> None:
        self._connect = connect

    # ------------------------------------------------------------ helpers
    def _tx(self) -> AbstractContextManager[sqlite3.Connection]:
        return self._connect()

    @staticmethod
    def _flow_row(row: sqlite3.Row, *, graph: bool = True) -> dict[str, Any]:
        out = {"id": row["id"], "name": row["name"], "description": row["description"],
               "version": row["version"], "created_at": row["created_at"], "updated_at": row["updated_at"],
               "template_id": row["template_id"], "owner": row["owner"]}
        if graph:
            out["graph"] = _loads(row["graph"], {})
        else:
            g = _loads(row["graph"], {})
            out["node_count"] = len(g.get("nodes") or [])
            out["node_types"] = sorted({n.get("type") for n in g.get("nodes") or []})[:20]
        return out

    # -------------------------------------------------------------- flows
    def create_flow(self, graph: dict, *, owner: str, author: str, template_id: str | None = None) -> dict:
        fid = new_flow_id()
        now = time.time()
        with self._tx() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("INSERT INTO flow_flows (id, owner, name, description, version, graph, created_at, "
                        "updated_at, template_id) VALUES (?,?,?,?,?,?,?,?,?)",
                        (fid, owner, graph["name"], graph.get("description", ""), 1, _dumps(graph), now, now,
                         template_id))
            con.execute("INSERT INTO flow_versions (flow_id, version, graph, name, created_at, author) "
                        "VALUES (?,?,?,?,?,?)", (fid, 1, _dumps(graph), graph["name"], now, author))
            con.execute("COMMIT")
        return self.get_flow(fid)

    def get_flow(self, flow_id: str, *, owner: str | None = None) -> dict:
        if not FLOW_ID.fullmatch(flow_id or ""):
            raise FlowError("invalid flow id", 404, "not_found")
        with self._tx() as con:
            row = con.execute("SELECT * FROM flow_flows WHERE id=? AND deleted=0", (flow_id,)).fetchone()
        if row is None or (owner is not None and row["owner"] != owner):
            raise FlowError("no such flow", 404, "not_found")
        return self._flow_row(row)

    def list_flows(self, *, owner: str | None = None, q: str = "", limit: int = 100) -> list[dict]:
        where, args = ["deleted=0"], []
        if owner is not None:
            where.append("owner=?")
            args.append(owner)
        if q:
            if len(q) > 120:
                raise FlowError("search text is too long")
            where.append("(name LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\')")
            like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            args += [like, like]
        limit = max(1, min(500, int(limit)))
        with self._tx() as con:
            rows = con.execute(f"SELECT * FROM flow_flows WHERE {' AND '.join(where)} "  # noqa: S608
                               "ORDER BY updated_at DESC LIMIT ?", (*args, limit)).fetchall()
            last_runs = {r["flow_id"]: dict(r) for r in con.execute(
                "SELECT flow_id, id, status, created_at, finished_at FROM flow_runs r WHERE created_at = "
                "(SELECT MAX(created_at) FROM flow_runs x WHERE x.flow_id = r.flow_id)")}
        out = []
        for r in rows:
            item = self._flow_row(r, graph=False)
            item["last_run"] = last_runs.get(r["id"])
            out.append(item)
        return out

    def update_flow(self, flow_id: str, graph: dict, *, base_version: int, author: str,
                    owner: str | None = None) -> dict:
        now = time.time()
        with self._tx() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM flow_flows WHERE id=? AND deleted=0", (flow_id,)).fetchone()
            if row is None or (owner is not None and row["owner"] != owner):
                con.execute("ROLLBACK")
                raise FlowError("no such flow", 404, "not_found")
            if row["version"] != base_version:
                con.execute("ROLLBACK")
                raise FlowError(f"the flow was changed elsewhere (now version {row['version']}, you edited "
                                f"{base_version}); reload it", 409, "version_conflict")
            if _loads(row["graph"], {}) == graph:
                con.execute("ROLLBACK")
                return self._flow_row(row)
            version = row["version"] + 1
            con.execute("UPDATE flow_flows SET name=?, description=?, version=?, graph=?, updated_at=? WHERE id=?",
                        (graph["name"], graph.get("description", ""), version, _dumps(graph), now, flow_id))
            con.execute("INSERT INTO flow_versions (flow_id, version, graph, name, created_at, author) "
                        "VALUES (?,?,?,?,?,?)", (flow_id, version, _dumps(graph), graph["name"], now, author))
            con.execute("DELETE FROM flow_versions WHERE flow_id=? AND version <= ?",
                        (flow_id, version - MAX_VERSIONS_KEPT))
            con.execute("COMMIT")
        return self.get_flow(flow_id)

    def delete_flow(self, flow_id: str, *, owner: str | None = None) -> None:
        with self._tx() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT owner FROM flow_flows WHERE id=? AND deleted=0", (flow_id,)).fetchone()
            if row is None or (owner is not None and row["owner"] != owner):
                con.execute("ROLLBACK")
                raise FlowError("no such flow", 404, "not_found")
            active = con.execute("SELECT 1 FROM flow_runs WHERE flow_id=? AND status IN ('queued','running')",
                                 (flow_id,)).fetchone()
            if active:
                con.execute("ROLLBACK")
                raise FlowError("the flow is running; cancel the run first", 409, "busy")
            # Soft delete: run history and Library provenance keep their flow id.
            con.execute("UPDATE flow_flows SET deleted=1, updated_at=? WHERE id=?", (time.time(), flow_id))
            con.execute("COMMIT")

    def versions(self, flow_id: str) -> list[dict]:
        with self._tx() as con:
            rows = con.execute("SELECT version, name, created_at, author, length(graph) AS bytes FROM "
                               "flow_versions WHERE flow_id=? ORDER BY version DESC LIMIT 200", (flow_id,)).fetchall()
        return [dict(r) for r in rows]

    def version(self, flow_id: str, version: int) -> dict:
        with self._tx() as con:
            row = con.execute("SELECT * FROM flow_versions WHERE flow_id=? AND version=?",
                              (flow_id, version)).fetchone()
        if row is None:
            raise FlowError("no such version", 404, "not_found")
        return {"version": row["version"], "name": row["name"], "created_at": row["created_at"],
                "author": row["author"], "graph": _loads(row["graph"], {})}

    # ---------------------------------------------------------- templates
    def upsert_builtin(self, tid: str, name: str, description: str, category: str, graph: dict) -> None:
        now = time.time()
        with self._tx() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT graph FROM flow_templates WHERE id=?", (tid,)).fetchone()
            if row is None:
                con.execute("INSERT INTO flow_templates (id, owner, name, description, category, graph, builtin, "
                            "created_at, updated_at) VALUES (?,?,?,?,?,?,1,?,?)",
                            (tid, "builtin", name, description, category, _dumps(graph), now, now))
            elif row["graph"] != _dumps(graph):
                con.execute("UPDATE flow_templates SET name=?, description=?, category=?, graph=?, updated_at=? "
                            "WHERE id=?", (name, description, category, _dumps(graph), now, tid))
            con.execute("COMMIT")

    def create_template(self, graph: dict, *, owner: str, name: str, description: str, category: str) -> dict:
        tid = new_template_id()
        now = time.time()
        with self._tx() as con:
            con.execute("INSERT INTO flow_templates (id, owner, name, description, category, graph, builtin, "
                        "created_at, updated_at) VALUES (?,?,?,?,?,?,0,?,?)",
                        (tid, owner, name, description, category, _dumps(graph), now, now))
        return self.get_template(tid)

    def get_template(self, tid: str, *, owner: str | None = None) -> dict:
        if not TEMPLATE_ID.fullmatch(tid or ""):
            raise FlowError("invalid template id", 404, "not_found")
        with self._tx() as con:
            row = con.execute("SELECT * FROM flow_templates WHERE id=?", (tid,)).fetchone()
        if row is None or (owner is not None and not row["builtin"] and row["owner"] != owner):
            raise FlowError("no such template", 404, "not_found")
        return self._template_row(row)

    @staticmethod
    def _template_row(row: sqlite3.Row, graph: bool = True) -> dict:
        g = _loads(row["graph"], {})
        out = {"id": row["id"], "name": row["name"], "description": row["description"], "category": row["category"],
               "builtin": bool(row["builtin"]), "created_at": row["created_at"], "updated_at": row["updated_at"],
               "node_count": len(g.get("nodes") or []),
               "node_types": sorted({n.get("type") for n in g.get("nodes") or []})}
        if graph:
            out["graph"] = g
        return out

    def list_templates(self, *, owner: str | None = None) -> list[dict]:
        with self._tx() as con:
            if owner is None:
                rows = con.execute("SELECT * FROM flow_templates ORDER BY builtin DESC, updated_at DESC").fetchall()
            else:
                rows = con.execute("SELECT * FROM flow_templates WHERE builtin=1 OR owner=? "
                                   "ORDER BY builtin DESC, updated_at DESC", (owner,)).fetchall()
        return [self._template_row(r, graph=False) for r in rows]

    def delete_template(self, tid: str, *, owner: str | None = None) -> None:
        tpl = self.get_template(tid, owner=owner)
        if tpl["builtin"]:
            raise FlowError("built-in templates cannot be deleted; duplicate one to change it", 409, "builtin")
        with self._tx() as con:
            con.execute("DELETE FROM flow_templates WHERE id=? AND builtin=0", (tid,))

    # --------------------------------------------------------------- runs
    def create_run(self, *, flow: dict, owner: str, user: str, mode: str, target: str | None,
                   graph: dict, nodes: list[tuple[str, str]], parent_run: str | None) -> str:
        rid = new_run_id()
        now = time.time()
        with self._tx() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("INSERT INTO flow_runs (id, flow_id, flow_version, owner, user, mode, target_node, status, "
                        "graph, created_at, parent_run) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (rid, flow["id"], flow["version"], owner, user, mode, target, "queued", _dumps(graph), now,
                         parent_run))
            con.executemany("INSERT INTO flow_node_runs (run_id, node_id, node_type, status) VALUES (?,?,?,?)",
                            [(rid, nid, ntype, "pending") for nid, ntype in nodes])
            con.execute("COMMIT")
        return rid

    def update_run(self, run_id: str, **values: Any) -> None:
        allowed = {"status", "started_at", "finished_at", "error", "summary"}
        sets = []
        args: list[Any] = []
        for k, v in values.items():
            if k not in allowed:
                raise KeyError(k)
            sets.append(f"{k}=?")
            args.append(_dumps(v) if k == "summary" else v)
        with self._tx() as con:
            con.execute(f"UPDATE flow_runs SET {', '.join(sets)} WHERE id=?", (*args, run_id))  # noqa: S608

    def update_node(self, run_id: str, node_id: str, **values: Any) -> None:
        allowed = {"status", "detail", "started_at", "finished_at", "cache_key", "cached", "outputs", "error",
                   "logs", "payload", "model", "jobs", "resource", "progress"}
        sets = []
        args: list[Any] = []
        for k, v in values.items():
            if k not in allowed:
                raise KeyError(k)
            sets.append(f"{k}=?")
            if k in ("outputs", "logs", "payload", "jobs", "resource"):
                v = None if (k == "resource" and v is None) else _dumps(v)
            elif k == "cached":
                v = int(bool(v))
            elif k == "detail":
                v = str(v or "")[:500]
            args.append(v)
        with self._tx() as con:
            con.execute(f"UPDATE flow_node_runs SET {', '.join(sets)} WHERE run_id=? AND node_id=?",  # noqa: S608
                        (*args, run_id, node_id))

    @staticmethod
    def _run_row(row: sqlite3.Row, graph: bool = False) -> dict:
        out = {k: row[k] for k in ("id", "flow_id", "flow_version", "owner", "user", "mode", "target_node", "status",
                                   "created_at", "started_at", "finished_at", "error", "parent_run")}
        out["summary"] = _loads(row["summary"], {})
        end = row["finished_at"] or time.time()
        out["duration_s"] = round(end - row["started_at"], 1) if row["started_at"] else None
        if graph:
            out["graph"] = _loads(row["graph"], {})
        return out

    @staticmethod
    def _node_row(row: sqlite3.Row, full: bool = False) -> dict:
        out = {k: row[k] for k in ("node_id", "node_type", "status", "detail", "started_at", "finished_at",
                                   "cache_key", "error", "model", "progress")}
        out["cached"] = bool(row["cached"])
        out["outputs"] = _loads(row["outputs"], {})
        out["jobs"] = _loads(row["jobs"], [])
        out["resource"] = _loads(row["resource"], None)
        end = row["finished_at"] or (time.time() if row["started_at"] else None)
        out["duration_s"] = round(end - row["started_at"], 2) if row["started_at"] and end else None
        logs = _loads(row["logs"], [])
        out["log_count"] = len(logs)
        if full:
            out["logs"] = logs
            out["payload"] = _loads(row["payload"], {})
        return out

    def get_run(self, run_id: str, *, owner: str | None = None, graph: bool = False) -> dict:
        if not RUN_ID.fullmatch(run_id or ""):
            raise FlowError("invalid run id", 404, "not_found")
        with self._tx() as con:
            row = con.execute("SELECT * FROM flow_runs WHERE id=?", (run_id,)).fetchone()
            if row is None or (owner is not None and row["owner"] != owner):
                raise FlowError("no such run", 404, "not_found")
            nodes = con.execute("SELECT * FROM flow_node_runs WHERE run_id=? ORDER BY rowid", (run_id,)).fetchall()
        out = self._run_row(row, graph=graph)
        out["nodes"] = {n["node_id"]: self._node_row(n) for n in nodes}
        return out

    def node_detail(self, run_id: str, node_id: str, *, owner: str | None = None) -> dict:
        self.get_run(run_id, owner=owner)
        with self._tx() as con:
            row = con.execute("SELECT * FROM flow_node_runs WHERE run_id=? AND node_id=?",
                              (run_id, node_id)).fetchone()
        if row is None:
            raise FlowError("that node is not part of the run", 404, "not_found")
        return self._node_row(row, full=True)

    def append_log(self, run_id: str, node_id: str, line: str) -> None:
        entry = {"ts": round(time.time(), 3), "msg": line[:600]}
        with self._tx() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT logs FROM flow_node_runs WHERE run_id=? AND node_id=?",
                              (run_id, node_id)).fetchone()
            if row is None:
                con.execute("ROLLBACK")
                return
            logs = _loads(row["logs"], [])
            logs.append(entry)
            con.execute("UPDATE flow_node_runs SET logs=? WHERE run_id=? AND node_id=?",
                        (_dumps(logs[-MAX_LOG_LINES:]), run_id, node_id))
            con.execute("COMMIT")

    def list_runs(self, flow_id: str | None = None, *, owner: str | None = None, limit: int = 50) -> list[dict]:
        where, args = [], []
        if flow_id is not None:
            where.append("flow_id=?")
            args.append(flow_id)
        if owner is not None:
            where.append("owner=?")
            args.append(owner)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        limit = max(1, min(200, int(limit)))
        with self._tx() as con:
            rows = con.execute(f"SELECT * FROM flow_runs {clause} ORDER BY created_at DESC LIMIT ?",  # noqa: S608
                               (*args, limit)).fetchall()
            counts: dict[str, dict[str, int]] = {}
            ids = [r["id"] for r in rows]
            if ids:
                marks = ",".join("?" * len(ids))
                for r in con.execute(f"SELECT run_id, status, COUNT(*) AS n FROM flow_node_runs "  # noqa: S608
                                     f"WHERE run_id IN ({marks}) GROUP BY run_id, status", ids):
                    counts.setdefault(r["run_id"], {})[r["status"]] = r["n"]
        out = []
        for r in rows:
            item = self._run_row(r)
            item["node_counts"] = counts.get(r["id"], {})
            out.append(item)
        return out

    def active_runs(self) -> list[str]:
        with self._tx() as con:
            return [r[0] for r in con.execute("SELECT id FROM flow_runs WHERE status IN ('queued','running') "
                                              "ORDER BY created_at")]

    def mark_interrupted(self) -> list[str]:
        """After a restart: nothing from before is still executing here."""
        now = time.time()
        with self._tx() as con:
            con.execute("BEGIN IMMEDIATE")
            ids = [r[0] for r in con.execute("SELECT id FROM flow_runs WHERE status IN ('queued','running')")]
            for rid in ids:
                con.execute("UPDATE flow_node_runs SET status='interrupted', finished_at=COALESCE(finished_at, ?), "
                            "detail='interrupted by a Control Center restart; use Rerun failed' "
                            "WHERE run_id=? AND status IN ('pending','queued','waiting','running')", (now, rid))
                con.execute("UPDATE flow_runs SET status='interrupted', finished_at=?, "
                            "error='The Control Center restarted while this run was active.' WHERE id=?", (now, rid))
            con.execute("COMMIT")
        return ids

    def iter_node_rows(self, run_id: str) -> Iterator[dict]:
        with self._tx() as con:
            rows = con.execute("SELECT * FROM flow_node_runs WHERE run_id=?", (run_id,)).fetchall()
        for r in rows:
            yield self._node_row(r, full=True)

    def runs_for_activity(self, since: float, limit: int) -> list[dict]:
        with self._tx() as con:
            rows = con.execute("SELECT r.*, f.name AS flow_name FROM flow_runs r LEFT JOIN flow_flows f "
                               "ON f.id = r.flow_id WHERE r.created_at >= ? ORDER BY r.created_at DESC LIMIT ?",
                               (since, max(1, min(200, limit)))).fetchall()
        return [{**self._run_row(r), "flow_name": r["flow_name"]} for r in rows]

    # -------------------------------------------------------------- cache
    def cache_get(self, key: str) -> dict | None:
        with self._tx() as con:
            row = con.execute("SELECT * FROM flow_cache WHERE cache_key=?", (key,)).fetchone()
            if row is None:
                return None
            con.execute("UPDATE flow_cache SET hits = hits + 1 WHERE cache_key=?", (key,))
        return {"outputs": _loads(row["outputs"], {}), "meta": _loads(row["meta"], {}), "run_id": row["run_id"],
                "node_id": row["node_id"], "created_at": row["created_at"]}

    def cache_put(self, key: str, *, node_type: str, outputs: dict, meta: dict, flow_id: str, node_id: str,
                  run_id: str) -> None:
        with self._tx() as con:
            con.execute("INSERT OR REPLACE INTO flow_cache (cache_key, node_type, outputs, meta, flow_id, node_id, "
                        "run_id, created_at, hits) VALUES (?,?,?,?,?,?,?,?,0)",
                        (key, node_type, _dumps(outputs), _dumps(meta), flow_id, node_id, run_id, time.time()))

    def cache_drop(self, key: str) -> None:
        with self._tx() as con:
            con.execute("DELETE FROM flow_cache WHERE cache_key=?", (key,))

    def last_outputs(self, flow_id: str, node_id: str) -> tuple[dict, str] | None:
        """The latest successful outputs of a node (used by locked nodes)."""
        with self._tx() as con:
            row = con.execute("SELECT n.outputs, n.run_id FROM flow_node_runs n JOIN flow_runs r ON r.id = n.run_id "
                              "WHERE r.flow_id=? AND n.node_id=? AND n.status IN ('succeeded','cached','reused') "
                              "ORDER BY n.finished_at DESC LIMIT 1", (flow_id, node_id)).fetchone()
        if row is None:
            return None
        return _loads(row["outputs"], {}), row["run_id"]

    # --------------------------------------------------------- provenance
    def tag_asset(self, asset_id: str, *, flow_id: str, run_id: str, node_id: str) -> None:
        """Record which flow node produced a Library row (only where unset)."""
        with self._tx() as con:
            con.execute("UPDATE assets SET flow_id=COALESCE(flow_id, ?), flow_run_id=COALESCE(flow_run_id, ?), "
                        "flow_node_id=COALESCE(flow_node_id, ?) WHERE id=?", (flow_id, run_id, node_id, asset_id))

    def asset_owned(self, asset_id: str, owner: str) -> bool:
        with self._tx() as con:
            row = con.execute("SELECT 1 FROM assets a JOIN flow_runs r ON r.id = a.flow_run_id "
                              "WHERE a.id=? AND r.owner=?", (asset_id, owner)).fetchone()
        return row is not None

    def asset_sha(self, ids: list[str]) -> dict[str, str | None]:
        ids = [i for i in dict.fromkeys(ids) if isinstance(i, str)]
        if not ids:
            return {}
        with self._tx() as con:
            rows = con.execute(f"SELECT id, sha256 FROM assets WHERE id IN ({','.join('?' * len(ids))})",  # noqa: S608
                               ids).fetchall()
        found = {r["id"]: r["sha256"] for r in rows}
        return {i: found.get(i) for i in ids}
