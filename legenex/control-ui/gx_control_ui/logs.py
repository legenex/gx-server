"""Predefined, read-only log streams.

The stream inventory comes from the CONFIG (config.log_sources()) — the V4.1
source list including the scheduler history JSONL, the orchestrator log and
the mia /srv/logs/dsv41-* logs once they exist. No per-feature source is
hardcoded here.

The browser can only name a stream id from that list; it cannot supply a
path, a container name or a command. Every line returned is redacted.
"""

from __future__ import annotations

import glob
import os
import shlex
import time
from dataclasses import dataclass

from .config import UIConfig
from .redact import redact
from .util import run, ssh_args

MIN_LINES, MAX_LINES, DEFAULT_LINES = 10, 2000, 200
MAX_QUERY = 200


@dataclass(frozen=True)
class Stream:
    id: str
    label: str
    node: str          # node1 | node2
    kind: str          # file | glob | docker | journal
    target: str
    group: str

    def as_dict(self) -> dict:
        return {"id": self.id, "label": self.label, "node": self.node, "kind": self.kind,
                "source": self.target, "group": self.group}


def streams(cfg: UIConfig) -> tuple[Stream, ...]:
    """The deployment's stream list, built from config.log_source_list()."""
    out = []
    for src in cfg.log_source_list():
        out.append(Stream(str(src["id"]), str(src["label"]), str(src["node"]), str(src["kind"]),
                          str(src["target"]), str(src.get("group", ""))))
    return tuple(out)


def stream_by_id(cfg: UIConfig, stream_id: str) -> Stream | None:
    return next((s for s in streams(cfg) if s.id == stream_id), None)


def clamp_lines(value) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_LINES
    return max(MIN_LINES, min(MAX_LINES, n))


def tail_file(path: str, lines: int, max_bytes: int = 4 * 1024 * 1024) -> list[str]:
    """Last `lines` lines of a file without reading the whole thing."""
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        end = fh.tell()
        block, data = 65536, b""
        pos = end
        while pos > 0 and data.count(b"\n") <= lines and end - pos < max_bytes:
            step = min(block, pos)
            pos -= step
            fh.seek(pos)
            data = fh.read(step) + data
    text = data.decode("utf-8", "replace").splitlines()
    return text[-lines:]


def _latest(pattern: str) -> str | None:
    matches = glob.glob(pattern)
    return max(matches, key=os.path.getmtime) if matches else None


def read_stream(cfg: UIConfig, stream_id: str, lines, query: str = "") -> dict:
    stream = stream_by_id(cfg, stream_id)
    if stream is None:
        raise KeyError(stream_id)
    n = clamp_lines(lines)
    query = (query or "")[:MAX_QUERY]
    source = stream.target
    t0 = time.time()
    error = ""
    out: list[str] = []

    if stream.node == "node1":
        if stream.kind in ("file", "glob"):
            path = _latest(stream.target) if stream.kind == "glob" else stream.target
            source = path or stream.target
            try:
                if not path:
                    raise FileNotFoundError(stream.target)
                out = tail_file(path, n)
            except FileNotFoundError:
                error = "log file does not exist (yet)"
            except OSError as exc:
                error = f"cannot read: {exc.strerror}"
        elif stream.kind == "docker":
            res = run(["docker", "logs", "--tail", str(n), "--timestamps", stream.target], timeout=15)
            out = res.out.splitlines()[-n:]
            if not res.ok:
                error = "container not found or not readable"
        elif stream.kind == "journal":
            res = run(["journalctl", "--user", "-u", stream.target, "-n", str(n), "--no-pager",
                       "-o", "short-iso"], timeout=15)
            out = res.out.splitlines()[-n:]
    else:
        if cfg.offline:
            error = "offline mode"
        else:
            if stream.kind == "file":
                remote = f"tail -n {n} -- {shlex.quote(stream.target)}"
            elif stream.kind == "docker":
                remote = f"docker logs --tail {n} --timestamps {shlex.quote(stream.target)} 2>&1"
            else:
                remote = "true"
            res = run(ssh_args(cfg.node2_ssh) + [remote], timeout=20)
            out = res.out.splitlines()[-n:]
            if not res.ok:
                error = "not available on gx10-02 (missing file/container or SSH failure)"
    if query:
        q = query.lower()
        out = [line for line in out if q in line.lower()]
    return {
        "stream": stream.as_dict(),
        "source": source,
        "lines": [redact(line[:4000]) for line in out],
        "count": len(out),
        "requested": n,
        "query": query,
        "error": error,
        "fetched_at": time.time(),
        "ms": round((time.time() - t0) * 1000),
    }
