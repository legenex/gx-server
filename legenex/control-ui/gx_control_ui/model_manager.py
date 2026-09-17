"""Model Manager (D-034): inventory, staging, testing, alias assignment,
rollback and removal of model files on both nodes.

Replacement workflow (the order is the safety property):

    lookup -> resolve exact SHA -> plan (disk / memory / runtime / node)
    -> stage: download the pinned revision into the target directory
    -> verify: size + sha256 of every file, write .gx-manifest.json
    -> test: start a TEMPORARY container (admission-checked) and run a real
       completion; the production alias is untouched
    -> assign: back up the binding, edit it, restart that node's llama-swap,
       run a real completion through LiteLLM; on failure restore the backup
       automatically; on success keep the backup as the rollback point
    -> accept: drop the rollback point
    -> delete: only files no alias, workflow or config references

Safety rules:
* A repository id, revision and path never reach a shell unvalidated; every
  command is a fixed argument list (node 2: a fixed script with shlex-quoted,
  pre-validated arguments). The HF token reaches `hf` through the
  environment (node 1) or stdin (node 2), never argv.
* Nothing from a repository is executed by the manager. Runtimes load
  weights; `trust_remote_code` checkpoints are flagged in the UI.
* Paths are confined to /srv/models/{gguf,vllm,deepseek,staging}/<name>.
* Deleting refuses anything a current binding, a rollback point, gx-max.conf
  or a media workflow references.
"""

from __future__ import annotations

import collections
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .hf import HFClient, HFError, SHA_RE, REPO_RE
from .redact import redact
from .util import HTTPError, bearer, http, http_json, run, ssh_args

log = logging.getLogger("gx.ui.models")

MODELS_ROOT = Path("/srv/models")
CATEGORIES = ("gguf", "vllm", "deepseek", "staging")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
NODES = ("node1", "node2")
TEXT_ALIASES = ("gx-mini", "gx-fast", "gx-reason", "gx-max")
ALIAS_NODE = {"gx-mini": "node1", "gx-fast": "node1", "gx-reason": "node2", "gx-max": "both"}
GIB = 2**30

#: Inventory script executed on node 2 over SSH (fixed text, no arguments).
_NODE2_INVENTORY = r"""
import json, os, pathlib, shutil
root = pathlib.Path("/srv/models")
out = {"dirs": [], "manifests": [], "disk": {}}
u = shutil.disk_usage(str(root))
out["disk"] = {"total": u.total, "free": u.free, "used": u.used}
for cat in ("gguf", "vllm", "deepseek", "staging"):
    base = root / cat
    if not base.is_dir():
        continue
    for d in sorted(base.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        size = 0
        for p in d.rglob("*"):
            try:
                if p.is_file() and ".cache" not in p.parts:
                    size += p.stat().st_size
            except OSError:
                pass
        man = None
        try:
            man = json.loads((d / ".gx-manifest.json").read_text())
        except Exception:
            pass
        out["dirs"].append({"category": cat, "name": d.name, "path": str(d), "size": size,
                            "manifest": {k: man.get(k) for k in ("repository", "revision", "verified_at", "gated")} if man else None})
mdir = root / "manifests"
if mdir.is_dir():
    for f in sorted(mdir.glob("*.json")):
        try:
            man = json.loads(f.read_text())
            files = [{"path": x.get("path"), "local_path": x.get("local_path"), "size": x.get("size"),
                      "present": os.path.exists(x.get("local_path") or "")} for x in man.get("files", [])]
            out["manifests"].append({"name": f.stem, "repository": man.get("repository"),
                                     "revision": man.get("revision"), "verified_at": man.get("verified_at"),
                                     "files": files})
        except Exception:
            pass
print(json.dumps(out))
"""


class ManagerError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class MMJob:
    id: str
    kind: str          # stage | test | assign | rollback | accept | delete
    label: str
    user: str
    params: dict
    started: float = field(default_factory=time.time)
    ended: float | None = None
    state: str = "running"   # running | succeeded | failed
    progress: float | None = None
    output: list[str] = field(default_factory=list)
    result: dict = field(default_factory=dict)

    def log(self, line: str) -> None:
        for part in str(line).splitlines() or [""]:
            self.output.append(redact(part)[:1500])
        del self.output[:-400]

    def public(self, with_output: bool = True) -> dict:
        d = {"id": self.id, "kind": self.kind, "label": self.label, "user": self.user, "params": self.params,
             "started": self.started, "ended": self.ended, "state": self.state, "progress": self.progress,
             "elapsed_seconds": round((self.ended or time.time()) - self.started, 1), "result": self.result}
        if with_output:
            d["output"] = list(self.output)
        return d


def validate_target(category: str, name: str) -> Path:
    if category not in CATEGORIES:
        raise ManagerError(f"category must be one of {', '.join(CATEGORIES)}")
    if not NAME_RE.match(name or "") or ".." in name:
        raise ManagerError("invalid directory name")
    return MODELS_ROOT / category / name


def default_target(info: dict) -> tuple[str, str]:
    repo = info["repository"]
    name = repo.split("/", 1)[1]
    if "gguf" in info.get("formats", []):
        return "gguf", name
    if "SGLang TP=2 (gx-max)" in info.get("runtimes", []):
        return "deepseek", name
    return "vllm", name


# ------------------------------------------------------------------ bindings
_MACRO_LINE = re.compile(r'^(?P<indent>\s*)(?P<name>[A-Za-z_][A-Za-z0-9_]*):\s*"(?P<value>[^"]*)"(?P<rest>.*)$')
_CONF_LINE = re.compile(r'^(?P<name>GXMAX_[A-Z_]+)="\$\{(?P=name):-(?P<value>[^}]*)\}"(?P<rest>.*)$')


def read_macros(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        m = _MACRO_LINE.match(line) or _CONF_LINE.match(line)
        if m:
            out[m.group("name")] = m.group("value")
    return out


def set_macros(text: str, values: dict[str, str]) -> str:
    lines = text.splitlines(keepends=True)
    seen = set()
    for i, line in enumerate(lines):
        body = line.rstrip("\n")
        m = _MACRO_LINE.match(body)
        if m and m.group("name") in values:
            lines[i] = f'{m.group("indent")}{m.group("name")}: "{values[m.group("name")]}"{m.group("rest")}\n'
            seen.add(m.group("name"))
            continue
        m = _CONF_LINE.match(body)
        if m and m.group("name") in values:
            name = m.group("name")
            lines[i] = f'{name}="${{{name}:-{values[name]}}}"{m.group("rest")}\n'
            seen.add(name)
    missing = set(values) - seen
    if missing:
        raise ManagerError(f"binding macros not found: {', '.join(sorted(missing))}")
    return "".join(lines)


def write_in_place(path: Path, text: str) -> None:
    """Rewrite a bind-mounted config file without changing its inode."""
    with open(path, "r+", encoding="utf-8") as fh:
        fh.seek(0)
        fh.write(text)
        fh.truncate()
        fh.flush()
        os.fsync(fh.fileno())


class ModelManager:
    def __init__(self, cfg, cluster, hf: HFClient, audit=None) -> None:
        self.cfg = cfg
        self.cluster = cluster
        self.hf = hf
        self.audit = audit or (lambda **kw: None)
        self.repo_root: Path = cfg.repo_root
        self.registry_path = self.repo_root / "legenex" / "models" / "registry.json"
        self.state_dir: Path = cfg.gx_state_root / "model-manager"
        self.backup_dir = self.state_dir / "rollback"
        self.hf_bin = Path(os.environ.get("GX_HF_BIN", str(Path.home() / ".venvs" / "hf-download" / "bin" / "hf")))
        self.node2_hf_bin = os.environ.get("GX_NODE2_HF_BIN", "~/.venvs/hf-download/bin/hf")
        self._jobs: collections.OrderedDict[str, MMJob] = collections.OrderedDict()
        self._lock = threading.Lock()
        self._busy: str | None = None

    # ============================================================== state
    def registry(self) -> dict:
        try:
            return json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"aliases": {}}

    def _save_registry(self, data: dict) -> None:
        text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        tmp = self.registry_path.with_name(".registry.json.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.registry_path)

    def rollback_points(self) -> dict:
        try:
            return json.loads((self.state_dir / "rollback.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_rollback_points(self, data: dict) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "rollback.json").write_text(json.dumps(data, indent=1), encoding="utf-8")

    # ============================================================== jobs
    def jobs(self) -> list[dict]:
        with self._lock:
            return [j.public(with_output=False) for j in reversed(self._jobs.values())]

    def job(self, job_id: str) -> dict:
        with self._lock:
            j = self._jobs.get(job_id)
        if j is None:
            raise ManagerError("no such job", 404)
        return j.public()

    def _start(self, kind: str, label: str, user: str, params: dict, fn) -> dict:
        with self._lock:
            if self._busy:
                busy = self._jobs.get(self._busy)
                raise ManagerError(f"'{busy.label if busy else 'another job'}' is still running", 409)
            job = MMJob(secrets.token_hex(8), kind, label, user, params)
            self._jobs[job.id] = job
            while len(self._jobs) > 50:
                self._jobs.popitem(last=False)
            self._busy = job.id

        def runner() -> None:
            ok = False
            try:
                ok = bool(fn(job))
            except (ManagerError, HFError) as exc:
                job.log(f"refused: {exc}")
            except Exception as exc:  # noqa: BLE001
                log.exception("model manager job %s failed", job.id)
                job.log(f"error: {type(exc).__name__}: {exc}")
            finally:
                job.state = "succeeded" if ok else "failed"
                job.ended = time.time()
                with self._lock:
                    self._busy = None
                self.audit(user=user, ip="", action=f"models.{kind}", outcome=job.state, job=job.id,
                           params={k: v for k, v in params.items() if k != "token"})
        self.audit(user=user, ip="", action=f"models.{kind}", outcome="started", job=job.id)
        threading.Thread(target=runner, daemon=True, name=f"mm-{kind}").start()
        return job.public()

    # ============================================================== inventory
    def _node1_inventory(self) -> dict:
        out: dict[str, Any] = {"dirs": [], "manifests": []}
        u = shutil.disk_usage(MODELS_ROOT)
        out["disk"] = {"total": u.total, "free": u.free, "used": u.used}
        for cat in CATEGORIES:
            base = MODELS_ROOT / cat
            if not base.is_dir():
                continue
            for d in sorted(base.iterdir()):
                if not d.is_dir() or d.name.startswith("."):
                    continue
                size = 0
                for p in d.rglob("*"):
                    try:
                        if p.is_file() and ".cache" not in p.parts:
                            size += p.stat().st_size
                    except OSError:
                        pass
                man = None
                try:
                    raw = json.loads((d / ".gx-manifest.json").read_text())
                    man = {k: raw.get(k) for k in ("repository", "revision", "verified_at", "gated")}
                except (OSError, ValueError):
                    pass
                out["dirs"].append({"category": cat, "name": d.name, "path": str(d), "size": size,
                                    "manifest": man})
        return out

    def _node2_inventory(self) -> dict:
        res = run(ssh_args(self.cfg.node2_ssh, 10) + ["python3", "-"], timeout=120,
                  input_text=_NODE2_INVENTORY, merge_stderr=False)
        if not res.ok:
            return {"error": "gx10-02 unreachable", "dirs": [], "manifests": []}
        try:
            return json.loads(res.out)
        except ValueError:
            return {"error": "bad inventory output", "dirs": [], "manifests": []}

    def references(self) -> dict[str, list[str]]:
        """path (or media file name) -> what references it."""
        refs: dict[str, list[str]] = collections.defaultdict(list)
        reg = self.registry()
        for alias, a in (reg.get("aliases") or {}).items():
            if a.get("path"):
                refs[a["path"]].append(f"{alias} (current)")
            prev = a.get("previous") or {}
            if prev.get("path"):
                refs[prev["path"]].append(f"{alias} (rollback until cleanup is approved)")
        for alias, point in self.rollback_points().items():
            if point.get("previous_path"):
                refs[point["previous_path"]].append(f"{alias} (rollback point)")
        # live bindings
        n1 = read_macros((self.repo_root / "legenex/gateway/llama-swap/node01.yaml").read_text())
        n2 = read_macros((self.repo_root / "legenex/gateway/llama-swap/node02.yaml").read_text())
        conf = read_macros((self.repo_root / "legenex/lifecycle/gx-max.conf").read_text())
        for key, label in (("gx_mini_model", "gx-mini binding"), ("gx_fast_model_dir", "gx-fast binding")):
            v = n1.get(key, "")
            if v.startswith("/models/"):
                refs[str(MODELS_ROOT / Path(v[len('/models/'):]).parts[0] / Path(v[len('/models/'):]).parts[1])].append(label)
        v = n2.get("gx_reason_model_dir", "")
        if v.startswith("/models/"):
            rel = Path(v[len("/models/"):])
            refs[str(MODELS_ROOT / rel.parts[0] / rel.parts[1])].append("gx-reason binding")
        if conf.get("GXMAX_MODEL_DIR"):
            refs[conf["GXMAX_MODEL_DIR"]].append("gx-max binding (gx-max.conf)")
        for wf in sorted((self.repo_root / "legenex/media/workflows").glob("*.api.json")):
            try:
                meta = json.loads(wf.read_text()).get("_gx", {})
            except ValueError:
                continue
            for m in meta.get("models") or []:
                refs[m].append(f"workflow {wf.name.removesuffix('.api.json')}")
        return refs

    def inventory(self) -> dict:
        n1 = self._node1_inventory()
        n2 = self._node2_inventory()
        refs = self.references()
        reg = self.registry()
        installed = []
        for node, data in (("node1", n1), ("node2", n2)):
            for d in data.get("dirs", []):
                used_by = refs.get(d["path"], [])
                man = d.get("manifest") or {}
                aliases = [a for a, spec in (reg.get("aliases") or {}).items() if spec.get("path") == d["path"]]
                facts = (reg.get("aliases") or {}).get(aliases[0], {}) if aliases else {}
                installed.append({
                    "node": node, "category": d["category"], "name": d["name"], "path": d["path"],
                    "size": d["size"], "repository": man.get("repository") or facts.get("repository"),
                    "revision": man.get("revision") or facts.get("revision"),
                    "verified": bool(man.get("revision")), "verified_at": man.get("verified_at"),
                    "aliases": aliases, "referenced_by": used_by, "deletable": not used_by,
                    "runtime": facts.get("runtime"), "context": facts.get("context"),
                    "vision": facts.get("vision"), "tools": facts.get("tools"),
                    "reasoning": facts.get("reasoning"), "quantization": facts.get("quantization"),
                    "parameters": facts.get("parameters"), "active_parameters": facts.get("active_parameters"),
                    "uncensored": facts.get("uncensored"),
                    "url": f"https://huggingface.co/{man.get('repository') or facts.get('repository')}"
                    if (man.get("repository") or facts.get("repository")) else None,
                })
            for m in data.get("manifests", []):
                files = m.get("files") or []
                used = sorted({r for f in files for r in refs.get(Path(f.get("local_path") or "").name, [])})
                installed.append({
                    "node": node, "category": "media", "name": m["name"], "path": ", ".join(
                        f.get("local_path") or "" for f in files), "size": sum(f.get("size") or 0 for f in files),
                    "repository": m.get("repository"), "revision": m.get("revision"), "verified": True,
                    "verified_at": m.get("verified_at"), "aliases": [], "referenced_by": used,
                    "deletable": False, "kind": "media component",
                    "url": f"https://huggingface.co/{m.get('repository')}",
                })
        return {"installed": installed, "disk": {"node1": n1.get("disk"), "node2": n2.get("disk")},
                "node2_error": n2.get("error"), "aliases": reg.get("aliases"),
                "rollback": self.rollback_points(), "hf_token": self.hf.token_state()}

    # ============================================================== lookup
    def lookup(self, ref: str) -> dict:
        info = self.hf.info(ref)
        category, name = default_target(info)
        info["suggested_target"] = {"category": category, "name": name,
                                    "path": str(validate_target(category, name))}
        info["suggested_node"] = {"gguf": "node1", "vllm": "node1", "deepseek": "both"}.get(category, "node1")
        info["memory_estimate_gib"] = round(info["size_bytes"] / GIB * 1.25 + 6, 1) if info["size_bytes"] else None
        readme = self.hf.readme(info["repository"], info["revision"]) if info.get("accessible") else ""
        info["readme_excerpt"] = readme[:6000]
        return info

    def plan(self, body: dict) -> dict:
        info = self.hf.info(f"{body.get('repository')}@{body.get('revision')}")
        if info["revision"] != body.get("revision"):
            raise ManagerError("the revision moved; look the model up again")
        node = body.get("node")
        if node not in NODES:
            raise ManagerError("node must be node1 or node2")
        category, name = body.get("category"), body.get("name")
        target = validate_target(str(category), str(name))
        include = _patterns(body.get("include"))
        exclude = _patterns(body.get("exclude"))
        size = _selected_size(info, include, exclude)
        inv = self._node1_inventory() if node == "node1" else self._node2_inventory()
        free = (inv.get("disk") or {}).get("free") or 0
        warnings = list(info.get("warnings") or [])
        if not info.get("accessible", True):
            warnings.append("Hugging Face refuses the download for this account (gated). Add a token with access "
                            "in Settings > Hugging Face.")
        if info["kind"] not in ("checkpoint", "comfyui_model"):
            warnings.append(f"This is a '{info['kind']}', not a servable checkpoint.")
        fits = size + 20 * GIB < free
        if not fits:
            warnings.append(f"Not enough disk on {node}: needs {size / GIB:.1f} GiB + 20 GiB margin, "
                            f"{free / GIB:.1f} GiB free.")
        exists = any(d["path"] == str(target) for d in inv.get("dirs", []))
        if exists:
            warnings.append(f"{target} already exists on {node}; staging resumes into it and re-verifies.")
        return {"repository": info["repository"], "revision": info["revision"], "node": node,
                "target": str(target), "category": category, "name": name, "include": include,
                "exclude": exclude, "download_bytes": size, "free_bytes": free, "fits": fits,
                "memory_estimate_gib": round(size / GIB * 1.25 + 6, 1), "runtimes": info.get("runtimes"),
                "candidate_aliases": info.get("candidate_aliases"), "trust_remote_code": info.get("trust_remote_code"),
                "gated": info.get("gated"), "accessible": info.get("accessible", True), "warnings": warnings,
                "ok": fits and info.get("accessible", True)}

    # ============================================================== stage
    def stage(self, body: dict, *, user: str) -> dict:
        plan = self.plan(body)
        if not plan["ok"]:
            raise ManagerError("; ".join(plan["warnings"]) or "the plan was refused")
        label = f"Install {plan['repository']}@{plan['revision'][:10]} on {plan['node']}"
        return self._start("stage", label, user, plan, lambda job: self._stage(job, plan))

    def _hf_env(self) -> dict:
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL")}
        env.update({"HF_HUB_DISABLE_TELEMETRY": "1", "HF_HOME": str(MODELS_ROOT / "staging" / ".hf-home"),
                    "HF_HUB_ENABLE_HF_TRANSFER": "0"})
        tok = self.hf.token()
        if tok:
            env["HF_TOKEN"] = tok
        return env

    def _download_args(self, bin_path: str, plan: dict) -> list[str]:
        args = [bin_path, "download", plan["repository"], "--revision", plan["revision"],
                "--local-dir", plan["target"], "--max-workers", "8"]
        for pat in plan["include"]:
            args += ["--include", pat]
        for pat in plan["exclude"]:
            args += ["--exclude", pat]
        return args

    def _stage(self, job: MMJob, plan: dict) -> bool:
        if not SHA_RE.match(plan["revision"]) or not REPO_RE.match(plan["repository"]):
            raise ManagerError("invalid plan")
        total = max(1, plan["download_bytes"])
        target = plan["target"]
        verify = [sys.executable, str(self.repo_root / "legenex/scripts/hf-verify.py"), plan["repository"],
                  plan["revision"], target]
        for pat in plan["include"]:
            verify += ["--include", pat]
        for pat in plan["exclude"]:
            verify += ["--exclude", pat]
        job.log(f"target {target} on {plan['node']}; {total / GIB:.2f} GiB")
        if plan["node"] == "node1":
            Path(target).mkdir(parents=True, exist_ok=True)
            proc = subprocess.Popen(self._download_args(str(self.hf_bin), plan), env=self._hf_env(),
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    text=True)
            while proc.poll() is None:
                job.progress = round(min(0.99, _dir_size(Path(target)) / total), 3)
                time.sleep(3)
            tail = (proc.stdout.read() if proc.stdout else "")[-2000:]
            job.log(tail)
            if proc.returncode != 0:
                job.log(f"download failed (exit {proc.returncode})")
                return False
            job.log("verifying sha256 of every file ...")
            res = run(verify + ["--jobs", "6"], timeout=7200)
            job.log(res.out[-2000:])
            job.progress = 1.0 if res.ok else job.progress
            job.result = {"verified": res.ok, "target": target, "node": "node1"}
            return res.ok
        # node 2: fixed script; token (if any) on stdin
        q = shlex.quote
        dl = " ".join(q(a) for a in self._download_args(self.node2_hf_bin, plan)).replace(
            q(self.node2_hf_bin), self.node2_hf_bin, 1)
        vf = " ".join(q(a) for a in [
            "python3", f"{self.cfg.node2_repo}/legenex/scripts/hf-verify.py", *verify[2:], "--jobs", "6"])
        script = (f"set -e; read -r HF_TOKEN || true; export HF_TOKEN; [ -n \"$HF_TOKEN\" ] || unset HF_TOKEN; "
                  f"export HF_HUB_DISABLE_TELEMETRY=1 HF_HOME=/srv/models/staging/.hf-home; "
                  f"mkdir -p {q(target)}; {dl} > /tmp/gx-mm-download.log 2>&1; tail -c 1500 /tmp/gx-mm-download.log; "
                  f"echo '== verify =='; {vf}")
        proc = subprocess.Popen(ssh_args(self.cfg.node2_ssh, 10) + ["bash", "-c", q(script)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        assert proc.stdin is not None
        proc.stdin.write((self.hf.token() or "") + "\n")
        proc.stdin.close()
        while proc.poll() is None:
            res = run(ssh_args(self.cfg.node2_ssh, 10) + [f"du -sb {q(target)} 2>/dev/null | cut -f1"], timeout=30)
            try:
                job.progress = round(min(0.99, int(res.out.strip() or 0) / total), 3)
            except ValueError:
                pass
            time.sleep(8)
        out = proc.stdout.read() if proc.stdout else ""
        job.log(out[-3000:])
        ok = proc.returncode == 0 and "VERIFIED" in out
        job.progress = 1.0 if ok else job.progress
        job.result = {"verified": ok, "target": target, "node": "node2"}
        return ok

    # ============================================================== test
    def test(self, body: dict, *, user: str) -> dict:
        node = body.get("node")
        if node not in NODES:
            raise ManagerError("node must be node1 or node2")
        path = Path(str(body.get("path") or ""))
        if path.parent.parent != MODELS_ROOT or path.parent.name not in ("gguf", "vllm", "staging"):
            raise ManagerError("only gguf/vllm/staging directories can be test-served")
        validate_target(path.parent.name, path.name)
        runtime = body.get("runtime")
        if runtime not in ("llama.cpp", "vllm"):
            raise ManagerError("runtime must be llama.cpp or vllm")
        label = f"Test-serve {path.name} ({runtime}) on {node}"
        params = {"node": node, "path": str(path), "runtime": runtime}
        return self._start("test", label, user, params, lambda job: self._test(job, node, path, runtime))

    def _test(self, job: MMJob, node: str, path: Path, runtime: str) -> bool:
        if self.cluster.gxmax_state() != "down":
            raise ManagerError("gx-max is not down; test-serving is refused while the cluster is taken over")
        name = "gx-mm-test"
        port = 19098
        if runtime == "llama.cpp":
            files = _gguf_files(path) if node == "node1" else self._remote_gguf(path)
            if not files["model"]:
                raise ManagerError("no .gguf model file in the directory")
            size_gib = files["size"] / GIB
            estimate = round(size_gib * 1.3 + 3, 1)
            cmd = ["docker", "run", "-d", "--rm", "--name", name, "--device", "nvidia.com/gpu=all",
                   "--network", "host", "--memory", f"{int(estimate + 8)}g", "--oom-score-adj", "900",
                   "-v", f"{path}:/m:ro", "legenex/llama-cpp-spark:latest",
                   "-m", f"/m/{files['model']}", "--host", "127.0.0.1", "--port", str(port),
                   "--ctx-size", "4096", "--n-gpu-layers", "99", "--jinja"]
            if files["mmproj"]:
                cmd += ["--mmproj", f"/m/{files['mmproj']}"]
            budget = 300
        else:
            size_gib = (_dir_size(path) if node == "node1" else self._remote_size(path)) / GIB
            estimate = round(size_gib * 1.2 + 12, 1)
            frac = min(0.6, max(0.2, round(estimate / 121.6, 2)))
            cmd = ["docker", "run", "-d", "--rm", "--name", name, "--device", "nvidia.com/gpu=all",
                   "--network", "host", "--ipc", "host", "--memory", f"{int(estimate + 16)}g",
                   "--oom-score-adj", "900", "-e", "HF_HUB_OFFLINE=1", "-v", f"{path}:/m:ro",
                   "jstarkg/vllm-gb10-flashnext:0.28-sm121-r6", "/m", "--served-model-name", "candidate",
                   "--host", "127.0.0.1", "--port", str(port), "--gpu-memory-utilization", str(frac),
                   "--max-model-len", "4096", "--max-num-seqs", "2", "--enforce-eager"]
            budget = 1800
        job.log(f"admission: estimated {estimate} GiB on {node}")
        verdict = self._admission(node, estimate)
        job.log(f"admission: {verdict.get('reason')}")
        if not verdict.get("allowed"):
            return False
        q = shlex.quote
        remote = node == "node2"

        def sh(args: list[str], timeout: float) -> Any:
            if remote:
                return run(ssh_args(self.cfg.node2_ssh, 10) + [" ".join(q(a) for a in args)], timeout=timeout)
            return run(args, timeout=timeout)

        sh(["docker", "rm", "-f", name], 60)
        started = time.time()
        res = sh(cmd, 120)
        job.log(res.out.strip()[-500:])
        if not res.ok:
            return False
        try:
            ok = False
            deadline = time.time() + budget
            while time.time() < deadline:
                probe = sh(["curl", "-fsS", "-m", "3", f"http://127.0.0.1:{port}/health"], 15)
                if probe.ok:
                    ok = True
                    break
                alive = sh(["docker", "inspect", "-f", "{{.State.Running}}", name], 15)
                if "true" not in alive.out:
                    job.log("the candidate container exited:")
                    job.log(sh(["docker", "logs", "--tail", "40", name], 30).out[-3000:])
                    return False
                job.progress = round(min(0.9, (time.time() - started) / budget), 2)
                time.sleep(5)
            if not ok:
                job.log("the candidate did not become healthy in time")
                return False
            load_s = round(time.time() - started, 1)
            body = json.dumps({"model": "candidate", "max_tokens": 32, "temperature": 0,
                               "messages": [{"role": "user", "content": "What is 12 times 12? Answer with the number."}]})
            t0 = time.time()
            chat = sh(["curl", "-fsS", "-m", "300", "-H", "Content-Type: application/json", "-d", body,
                       f"http://127.0.0.1:{port}/v1/chat/completions"], 320)
            try:
                answer = json.loads(chat.out)["choices"][0]["message"]
                text = (answer.get("content") or "") + " " + (answer.get("reasoning_content") or "")
            except (ValueError, KeyError, IndexError, TypeError):
                text = ""
            passed = "144" in text
            job.result = {"load_seconds": load_s, "answer": text.strip()[:200], "correct": passed,
                          "chat_seconds": round(time.time() - t0, 2), "node": node, "path": str(path)}
            job.log(f"real completion: {text.strip()[:200]!r} -> {'correct' if passed else 'WRONG'}")
            job.progress = 1.0
            self._record_test(str(path), node, job.result)
            return passed
        finally:
            stop = sh(["docker", "rm", "-f", name], 120)
            job.log(f"temporary container removed ({'ok' if stop.ok else stop.out.strip()[-200:]})")

    def _admission(self, node: str, estimate: float) -> dict:
        orch = self.repo_root / "legenex" / "orchestrator"
        if str(orch) not in sys.path:
            sys.path.insert(0, str(orch))
        from gx_orchestrator import resource_guard as rg  # noqa: PLC0415

        facts = self.cluster.node1.get(max_age=0) if node == "node1" else self.cluster.node2.get(max_age=0)
        avail = ((facts or {}).get("memory") or {}).get("MemAvailable")
        if not avail:
            return {"allowed": False, "reason": f"{node} memory facts unavailable"}
        res = rg.compute_admission(node, estimate, current_residency_gib=0.0, mem_available_gib=avail / GIB)
        return {"allowed": res.allowed, "reason": res.reason}

    def _remote_gguf(self, path: Path) -> dict:
        res = run(ssh_args(self.cfg.node2_ssh, 10) + [
            f"cd {shlex.quote(str(path))} && find . -name '*.gguf' -not -path './.cache/*' -printf '%s %P\\n'"],
            timeout=30)
        names = []
        size = 0
        for line in res.out.splitlines():
            parts = line.split(" ", 1)
            if len(parts) == 2 and parts[1].endswith(".gguf"):
                names.append(parts[1])
                size += int(parts[0])
        return _pick_gguf(sorted(names), size)

    def _remote_size(self, path: Path) -> int:
        res = run(ssh_args(self.cfg.node2_ssh, 10) + [f"du -sb {shlex.quote(str(path))} | cut -f1"], timeout=60)
        try:
            return int(res.out.strip())
        except ValueError:
            return 0

    def _record_test(self, path: str, node: str, result: dict) -> None:
        tests = {}
        try:
            tests = json.loads((self.state_dir / "tests.json").read_text())
        except (OSError, ValueError):
            pass
        tests[f"{node}:{path}"] = {**result, "at": time.time()}
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "tests.json").write_text(json.dumps(tests, indent=1))

    def last_tests(self) -> dict:
        try:
            return json.loads((self.state_dir / "tests.json").read_text())
        except (OSError, ValueError):
            return {}

    # ============================================================== assign
    def assign(self, body: dict, *, user: str) -> dict:
        alias = body.get("alias")
        if alias not in ("gx-mini", "gx-fast", "gx-reason"):
            raise ManagerError("the Model Manager assigns gx-mini, gx-fast and gx-reason; gx-max uses the "
                               "two-node procedure (see Docs > Model Manager)")
        path = Path(str(body.get("path") or ""))
        expected_cat = "gguf" if alias == "gx-mini" else "vllm"
        if path.parent != MODELS_ROOT / expected_cat:
            raise ManagerError(f"{alias} takes a directory under /srv/models/{expected_cat}")
        validate_target(expected_cat, path.name)
        dry_run = bool(body.get("dry_run"))
        tests = self.last_tests()
        node = ALIAS_NODE[alias]
        if not dry_run and not (tests.get(f"{node}:{path}") or {}).get("correct"):
            raise ManagerError("test-serve this model first; an alias is only assigned after a real completion")
        label = f"{'Preview' if dry_run else 'Assign'} {path.name} -> {alias}"
        params = {"alias": alias, "path": str(path), "dry_run": dry_run}
        return self._start("assign", label, user, params, lambda job: self._assign(job, alias, path, dry_run))

    def _binding(self, alias: str) -> tuple[Path, dict[str, str]]:
        if alias == "gx-reason":
            return self.repo_root / "legenex/gateway/llama-swap/node02.yaml", {}
        return self.repo_root / "legenex/gateway/llama-swap/node01.yaml", {}

    def _new_values(self, alias: str, path: Path) -> dict[str, str]:
        if alias == "gx-mini":
            files = _gguf_files(path)
            if not files["model"]:
                raise ManagerError("no .gguf model file in the directory")
            values = {"gx_mini_model": f"/models/gguf/{path.name}/{files['model']}"}
            if files["mmproj"]:
                values["gx_mini_mmproj"] = f"/models/gguf/{path.name}/{files['mmproj']}"
            return values
        if alias == "gx-fast":
            return {"gx_fast_model_dir": f"/models/vllm/{path.name}"}
        return {"gx_reason_model_dir": f"/models/vllm/{path.name}", "gx_reason_host_dir": path.name}

    def _assign(self, job: MMJob, alias: str, path: Path, dry_run: bool) -> bool:
        cfg_file, _ = self._binding(alias)
        original = cfg_file.read_text(encoding="utf-8")
        before = read_macros(original)
        values = self._new_values(alias, path) if alias != "gx-reason" else {
            "gx_reason_model_dir": f"/models/vllm/{path.name}", "gx_reason_host_dir": path.name}
        updated = set_macros(original, values)
        diff = [f"{k}: {before.get(k)!r} -> {v!r}" for k, v in values.items()]
        job.result = {"file": str(cfg_file.relative_to(self.repo_root)), "changes": diff}
        for line in diff:
            job.log(line)
        if dry_run:
            job.log("dry run: nothing written")
            return True
        if self.cluster.gxmax_state() != "down":
            raise ManagerError("gx-max is not down; assignments wait until it is released")
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%S")
        backup = self.backup_dir / f"{cfg_file.name}.{alias}.{stamp}"
        backup.write_text(original, encoding="utf-8")
        write_in_place(cfg_file, updated)
        job.log(f"binding written; backup {backup}")
        ok = self._restart_and_verify(job, alias)
        if not ok:
            job.log("verification FAILED: restoring the previous binding")
            write_in_place(cfg_file, original)
            self._restart_and_verify(job, alias)
            return False
        prev_dir = {"gx-mini": before.get("gx_mini_model", "").rsplit("/", 1)[0],
                    "gx-fast": before.get("gx_fast_model_dir", ""),
                    "gx-reason": before.get("gx_reason_model_dir", "")}[alias]
        points = self.rollback_points()
        points[alias] = {"backup": str(backup), "file": str(cfg_file), "previous_values": {k: before.get(k)
                         for k in values}, "previous_path": str(MODELS_ROOT / prev_dir[len("/models/"):])
                         if prev_dir.startswith("/models/") else None,
                         "new_path": str(path), "at": time.time(), "by": job.user}
        self._save_rollback_points(points)
        reg = self.registry()
        entry = (reg.setdefault("aliases", {})).setdefault(alias, {})
        manifest = _read_manifest(path) if ALIAS_NODE[alias] == "node1" else {}
        entry["previous"] = {"repository": entry.get("repository"), "revision": entry.get("revision"),
                             "path": entry.get("path"), "status": "rollback point (Model Manager)"}
        entry.update({"repository": manifest.get("repository") or entry.get("repository"),
                      "revision": manifest.get("revision") or entry.get("revision"),
                      "path": str(path), "assigned_by": job.user, "assigned_at": stamp})
        self._save_registry(reg)
        job.log("assigned; the previous model stays on disk as the rollback point until you accept")
        return True

    def _restart_and_verify(self, job: MMJob, alias: str) -> bool:
        node = ALIAS_NODE[alias]
        if node == "node2":
            if not self._sync_node2_gateway(job):
                return False
            res = run(ssh_args(self.cfg.node2_ssh, 10) + ["docker restart -t 30 gx-llama-swap-node02"], timeout=180)
        else:
            res = run(["docker", "restart", "-t", "30", "gx-llama-swap-node01"], timeout=180)
        job.log(f"llama-swap restart on {node}: {'ok' if res.ok else res.out[-300:]}")
        if not res.ok:
            return False
        key = self.cfg.secret("LITELLM_MASTER_KEY")
        deadline = time.time() + 2400
        while time.time() < deadline:
            try:
                status, data = http_json(
                    "POST", f"{self.cfg.litellm_base}/v1/chat/completions", headers=bearer(key), timeout=2400,
                    body={"model": alias, "max_tokens": 64, "temperature": 0,
                          "messages": [{"role": "user", "content": "What is 12 times 12? Answer with the number."}]})
            except HTTPError as exc:
                job.log(f"gateway not ready: {exc.message}")
                time.sleep(10)
                continue
            text = ""
            if isinstance(data, dict) and data.get("choices"):
                msg = data["choices"][0].get("message") or {}
                text = f"{msg.get('content') or ''} {msg.get('reasoning_content') or ''}"
            job.log(f"LiteLLM {alias}: HTTP {status} {text.strip()[:120]!r}")
            if status == 200:
                return "144" in text
            time.sleep(10)
        return False

    def _sync_node2_gateway(self, job: MMJob) -> bool:
        """gx10-02's llama-swap reads ~/gx-gateway/node02.yaml. Copy the committed file there once
        the pull-only checkout has it (autosync commits within ~1 minute)."""
        want = (self.repo_root / "legenex/gateway/llama-swap/node02.yaml").read_text()
        deadline = time.time() + 600
        while time.time() < deadline:
            res = run(ssh_args(self.cfg.node2_ssh, 10) + [
                f"cat {shlex.quote(self.cfg.node2_repo)}/legenex/gateway/llama-swap/node02.yaml"], timeout=30)
            if res.ok and res.out == want:
                cp = run(ssh_args(self.cfg.node2_ssh, 10) + [
                    f"cat {shlex.quote(self.cfg.node2_repo)}/legenex/gateway/llama-swap/node02.yaml "
                    "> ~/gx-gateway/node02.yaml.new && cat ~/gx-gateway/node02.yaml.new > ~/gx-gateway/node02.yaml "
                    "&& rm -f ~/gx-gateway/node02.yaml.new"], timeout=30)
                job.log("node02.yaml deployed to gx10-02" if cp.ok else cp.out[-300:])
                return cp.ok
            job.log("waiting for gx10-02's checkout to receive the binding (autosync)")
            time.sleep(20)
        job.log("gx10-02 did not receive the binding in 10 minutes")
        return False

    def rollback(self, body: dict, *, user: str) -> dict:
        alias = body.get("alias")
        point = self.rollback_points().get(alias)
        if not point:
            raise ManagerError("no rollback point for that alias")
        return self._start("rollback", f"Roll back {alias}", user, {"alias": alias},
                           lambda job: self._rollback(job, alias, point))

    def _rollback(self, job: MMJob, alias: str, point: dict) -> bool:
        cfg_file = Path(point["file"])
        text = cfg_file.read_text(encoding="utf-8")
        write_in_place(cfg_file, set_macros(text, {k: v for k, v in point["previous_values"].items() if v}))
        ok = self._restart_and_verify(job, alias)
        if ok:
            points = self.rollback_points()
            points.pop(alias, None)
            self._save_rollback_points(points)
            reg = self.registry()
            entry = reg["aliases"].get(alias, {})
            prev = entry.get("previous") or {}
            entry.update({"repository": prev.get("repository"), "revision": prev.get("revision"),
                          "path": prev.get("path")})
            entry.pop("previous", None)
            self._save_registry(reg)
        return ok

    def accept(self, body: dict, *, user: str) -> dict:
        alias = body.get("alias")
        points = self.rollback_points()
        reg = self.registry()
        prev = (reg.get("aliases", {}).get(alias) or {}).get("previous")
        if alias not in points and not prev:
            raise ManagerError("nothing to accept for that alias")
        if alias in points:
            points.pop(alias)
            self._save_rollback_points(points)
        if prev:
            prev["status"] = f"superseded (accepted {time.strftime('%Y-%m-%d')}); deletable"
            self._save_registry(reg)
        self.audit(user=user, ip="", action="models.accept", outcome="ok", alias=alias)
        return {"accepted": alias}

    # ============================================================== delete
    def delete(self, body: dict, *, user: str) -> dict:
        node = body.get("node")
        if node not in NODES:
            raise ManagerError("node must be node1 or node2")
        path = Path(str(body.get("path") or ""))
        if path.parent.parent != MODELS_ROOT or path.parent.name not in CATEGORIES:
            raise ManagerError("only model directories under /srv/models/{gguf,vllm,deepseek,staging} can be removed")
        validate_target(path.parent.name, path.name)
        if body.get("confirm") != path.name:
            raise ManagerError(f"type the directory name '{path.name}' to confirm")
        refs = self.references().get(str(path), [])
        reg = self.registry()
        for alias, a in (reg.get("aliases") or {}).items():
            prev = a.get("previous") or {}
            if prev.get("path") == str(path) and "accepted" in str(prev.get("status", "")):
                refs = [r for r in refs if not r.startswith(f"{alias} (rollback until")]
        if refs:
            raise ManagerError(f"refused: still referenced by {', '.join(refs)}", 409)
        label = f"Delete {path} on {node}"
        return self._start("delete", label, user, {"node": node, "path": str(path)},
                           lambda job: self._delete(job, node, path))

    def _delete(self, job: MMJob, node: str, path: Path) -> bool:
        if node == "node1":
            size = _dir_size(path)
            shutil.rmtree(path)
            ok = not path.exists()
        else:
            size = self._remote_size(path)
            res = run(ssh_args(self.cfg.node2_ssh, 10) + [
                f"rm -rf -- {shlex.quote(str(path))} && test ! -e {shlex.quote(str(path))}"], timeout=600)
            ok = res.ok
        job.result = {"freed_bytes": size if ok else 0}
        job.log(f"{'removed' if ok else 'FAILED to remove'} {path} ({size / GIB:.2f} GiB)")
        return ok

    # ============================================================== HF token
    def set_token(self, value: str, *, user: str) -> dict:
        state = self.hf.set_token(value)
        self.audit(user=user, ip="", action="hf.token.set", outcome="ok", hf_user=state.get("user"))
        return state

    def clear_token(self, *, user: str) -> dict:
        self.hf.clear_token()
        self.audit(user=user, ip="", action="hf.token.clear", outcome="ok")
        return {"configured": False}


# ------------------------------------------------------------------ helpers
def _patterns(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        value = [v.strip() for v in value.split(",")]
    if not isinstance(value, list) or len(value) > 20:
        raise ManagerError("file patterns must be a list")
    out = []
    for v in value:
        if not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9*?._/\[\]-]{1,120}", v) or ".." in v:
            raise ManagerError(f"invalid file pattern {v!r}")
        if v:
            out.append(v)
    return out


def _selected_size(info: dict, include: list[str], exclude: list[str]) -> int:
    import fnmatch

    total = 0
    for f in info.get("files") or []:
        if include and not any(fnmatch.fnmatch(f["path"], p) for p in include):
            continue
        if any(fnmatch.fnmatch(f["path"], p) for p in exclude):
            continue
        total += f.get("size") or 0
    return total


def _dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def _gguf_files(path: Path) -> dict:
    names = sorted(str(p.relative_to(path)) for p in path.rglob("*.gguf")
                   if ".cache" not in p.relative_to(path).parts)
    size = sum((path / n).stat().st_size for n in names)
    return _pick_gguf(names, size)


def _pick_gguf(names: list[str], size: int) -> dict:
    """`names` are paths relative to the model directory."""
    mmproj = next((n for n in names if Path(n).name.lower().startswith("mmproj")), None)
    models = [n for n in names if n != mmproj]
    # Prefer the first shard of a split model, else a Q4_K_M, else the smallest name.
    first = next((n for n in models if re.search(r"-0*1-of-\d+\.gguf$", n)), None)
    q4 = next((n for n in models if "q4_k_m" in n.lower()), None)
    return {"model": first or q4 or (models[0] if models else None), "mmproj": mmproj, "size": size}


def _read_manifest(path: Path) -> dict:
    try:
        return json.loads((path / ".gx-manifest.json").read_text())
    except (OSError, ValueError):
        return {}


def gateway_probe(cfg, alias: str) -> dict:
    """One real completion through LiteLLM (used by the inventory's 'Test' on an assigned alias)."""
    key = cfg.secret("LITELLM_MASTER_KEY")
    t0 = time.time()
    try:
        res = http("POST", f"{cfg.litellm_base}/v1/chat/completions", headers=bearer(key), timeout=900,
                   body={"model": alias, "max_tokens": 32, "temperature": 0,
                         "messages": [{"role": "user", "content": "What is 12 times 12? Answer with the number."}]})
        data = res.json()
    except (HTTPError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    text = ""
    if isinstance(data, dict) and data.get("choices"):
        msg = data["choices"][0].get("message") or {}
        text = f"{msg.get('content') or ''}"
    return {"ok": res.status == 200 and "144" in text, "status": res.status, "answer": text[:120],
            "seconds": round(time.time() - t0, 2)}
