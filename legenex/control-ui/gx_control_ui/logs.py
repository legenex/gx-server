"""Predefined, read-only log streams.

The browser can only name a stream id from `STREAMS`; it cannot supply a
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
_N2_HOME = "/home/legenex-02"


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


STREAMS: tuple[Stream, ...] = (
    Stream("orchestrator", "gx-orchestrator", "node1", "file", "/srv/logs/gx-orchestrator.log", "Control plane"),
    Stream("gxmax-lifecycle", "gx-max lifecycle (acquire/release output)", "node1", "file",
           "/srv/logs/gx-max-lifecycle.log", "gx-max"),
    Stream("litellm", "LiteLLM gateway", "node1", "docker", "gx-litellm", "Control plane"),
    Stream("swap-node1", "llama-swap node 1", "node1", "docker", "gx-llama-swap-node01", "Models"),
    Stream("swap-node2", "llama-swap node 2", "node2", "docker", "gx-llama-swap-node02", "Models"),
    Stream("gx-mini", "gx-mini container", "node1", "docker", "gx-mini", "Models"),
    Stream("gx-fast", "gx-fast container", "node1", "docker", "gx-fast", "Models"),
    Stream("gx-reason", "gx-reason container", "node2", "docker", "gx-reason", "Models"),
    Stream("rank0", "gx-max rank 0 (node 1)", "node1", "file", "/srv/logs/gx-max-rank0.log", "gx-max"),
    Stream("rank1", "gx-max rank 1 (node 2)", "node2", "file", f"{_N2_HOME}/gx-max-rank1.log", "gx-max"),
    Stream("gxmax-safety", "gx-max safety samples (node 1, latest run)", "node1", "glob",
           "/srv/logs/gx-max-safety-node1-*.tsv", "gx-max"),
    Stream("gxmax-node2-mem", "gx-max node 2 memory samples (latest run)", "node2", "file",
           f"{_N2_HOME}/gx-max-node2-mem.tsv", "gx-max"),
    Stream("rank0-watch", "rank 0 watcher", "node1", "file", "/srv/logs/gx-max-rank0-watch.log", "gx-max"),
    Stream("rank1-deadman", "rank 1 deadman", "node2", "file", f"{_N2_HOME}/gx-max-rank1-deadman.log", "gx-max"),
    Stream("media-router", "media router", "node2", "docker", "gx-media-router", "Media"),
    Stream("comfyui", "ComfyUI", "node2", "docker", "gx-comfyui", "Media"),
    Stream("git-autosync", "Git autosync (node 1)", "node1", "file",
           "/srv/logs/gx-git-sync/node1-autosync.log", "Git"),
    Stream("git-push-failures", "Git push failures (node 1)", "node1", "file",
           "/srv/logs/gx-git-sync/push-failures.log", "Git"),
    Stream("git-reconcile", "Git reconcile (node 2)", "node2", "file",
           "/srv/logs/gx-git-sync/node2-reconcile.log", "Git"),
    Stream("audit-node1", "Daily integrity audit (node 1)", "node1", "file",
           "/srv/logs/gx-git-sync/audit-latest.log", "Git"),
    Stream("audit-node2", "Daily integrity audit (node 2)", "node2", "file",
           "/srv/logs/gx-git-sync/audit-latest.log", "Git"),
    Stream("hostwatch-node1", "hostwatch (node 1)", "node1", "file", "/srv/logs/gx-hostwatch.log", "Host"),
    Stream("hostwatch-node2", "hostwatch (node 2)", "node2", "file", "/srv/logs/gx-hostwatch.log", "Host"),
    Stream("control-ui", "control UI service", "node1", "file",
           "/srv/logs/gx-control-ui/control-ui.log", "Control plane"),
    Stream("control-ui-audit", "control UI audit trail", "node1", "file",
           "/srv/logs/gx-control-ui/audit.log", "Control plane"),
)
BY_ID = {s.id: s for s in STREAMS}


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
    stream = BY_ID.get(stream_id)
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
