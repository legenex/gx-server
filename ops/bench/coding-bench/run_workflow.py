#!/usr/bin/env python3
"""Multi-agent coding benchmark for the GX Cluster (ops/bench/coding-bench).

Drives the workflow orchestrator -> inspector -> architect -> implementer ->
tester -> reviewer -> (repair) -> final validator through the gateway
(model gx-auto), with attribution headers:

    X-GX-Project: gx-coding-bench
    X-GX-Agent:    <role>
    X-GX-Task:     <run id>
    X-GX-Priority: normal-worker

Each role gets its prompt from templates/ (plain string templates, Jinja-free,
<<var>> replacement) and can act on a disposable sandboxed repo through a
STRICT harness: the model proposes shell commands / file writes, and the
harness executes ONLY what the allowlist permits:

    commands: pytest | python3 (in-repo scripts, -m pytest, -m unittest)
              git (status/diff/log/add/commit)
    cwd: pinned to the sandbox; relative paths only; no "..", no absolute
    paths, no shell metacharacters (no ; | & > < ` $ \ chaining), no rm,
    no curl/wget, no sudo — anything else is refused and fed back to the
    model so it can retry.

Everything is restartable (--sandbox reuses an existing repo, re-seeded only
with --reseed). Measured per run: wall time, LLM calls, total tokens,
per-call queue wait (ttfb proxy), inference concurrency observed (scheduler
poll), tool time, retries, errors, test results, final verdict.

Usage:
    run_workflow.py [--rounds N] [--label L] [--sandbox DIR] [--reseed]
                     [--gateway URL] [--report PATH]

Exit codes: 0 final verdict PASS on every round, 1 otherwise or on error.
Reports stay local under the state root (never pushed to the public repo).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
SEED_SCRIPT = HERE / "seed_repo.sh"
TEMPLATES = HERE / "templates"

GATEWAY_BASE = os.environ.get("GX_GATEWAY_BASE", "http://127.0.0.1:4000").rstrip("/")
MODEL = os.environ.get("GX_CODING_BENCH_MODEL", "gx-auto")
STATE_ROOT = Path(os.environ.get("GX_STATE_ROOT", "/srv/projects/gx-cluster/state"))
REPORT_DIR = Path(os.environ.get("GX_CODING_BENCH_REPORTS", str(STATE_ROOT / "bench" / "coding")))
ORCHESTRATOR_BASE = os.environ.get("GX_ORCH_BASE", "http://127.0.0.1:18900").rstrip("/")

REQUEST_TIMEOUT_S = float(os.environ.get("GX_BENCH_TIMEOUT_S", "900"))
COMMAND_TIMEOUT_S = float(os.environ.get("GX_CODING_BENCH_CMD_TIMEOUT_S", "120"))
MAX_TOOL_ROUNDS = 3
MAX_OUTPUT_CHARS = 4000
MAX_CONVERSATION_CHARS = 24000
HTTP_RETRIES = 3

PROJECT_HEADER = "gx-coding-bench"


class WorkflowError(Exception):
    """Operational error with a clear message."""


# ---------------------------------------------------------------------------
# Sandbox: strictly allowlisted command execution inside the repo
# ---------------------------------------------------------------------------

class SandboxRefusal(Exception):
    """A proposed command/write was refused (shown back to the model)."""


#: Shell metacharacters rejected ANYWHERE in a proposed single command.
_FORBIDDEN_CHARS = set(";|&><`$\\")
#: Leading programs that are allowed (basename of argv[0]).
_ALLOWED_PROGRAMS = {"pytest", "python3", "python", "git"}
#: git subcommands that are allowed (read + normal VCS hygiene).
_ALLOWED_GIT = {"status", "diff", "log", "add", "commit", "show"}


def _check_arg(arg: str) -> None:
    if not arg:
        return
    # an option like --output=/abs/path smuggles paths past a leading-dash
    # check -- validate the value after '=' as well.
    pieces = [arg]
    if "=" in arg:
        pieces.append(arg.split("=", 1)[1])
    for piece in pieces:
        if piece.startswith("/"):
            raise SandboxRefusal(f"absolute path in {arg!r} is not allowed; use paths relative to the repo root")
        if ".." in piece.split("/"):
            raise SandboxRefusal(f"path traversal ({arg!r}) is not allowed")
    for ch in arg:
        if ch in _FORBIDDEN_CHARS:
            raise SandboxRefusal(f"shell metacharacter {ch!r} is not allowed in {arg!r}")


def validate_command(command: str) -> list[str]:
    """Validate one proposed command, returning the argv to run (with argv[0]
    resolved to its full path if it is one of our interpreters).

    Raises SandboxRefusal with a reason for anything outside the allowlist.
    Deliberately paranoid: this runs model output.
    """
    command = command.strip()
    if not command:
        raise SandboxRefusal("empty command")
    if "\n" in command:
        raise SandboxRefusal("multi-line commands are not allowed; emit one command per ```bash block")
    for ch in command:
        if ch in _FORBIDDEN_CHARS:
            raise SandboxRefusal(f"shell metacharacter {ch!r} is not allowed (no pipes/chains/redirects)")
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise SandboxRefusal(f"unparsable command: {exc}") from None
    if not argv:
        raise SandboxRefusal("empty command")
    program = os.path.basename(argv[0])
    if program not in _ALLOWED_PROGRAMS:
        raise SandboxRefusal(
            f"{program!r} is not in the allowlist (pytest, python3, git) -- "
            "dangerous or out-of-scope commands are refused"
        )
    rest = argv[1:]
    if program == "git":
        if not rest or rest[0] not in _ALLOWED_GIT:
            raise SandboxRefusal(f"git {rest[0] if rest else ''!r} is not allowed (status/diff/log/add/commit/show only)")
        for arg in rest[1:]:
            _check_arg(arg)
        return ["git", *rest]
    if program in ("python3", "python"):
        if rest[:1] == ["-m"]:
            if len(rest) < 2:
                raise SandboxRefusal("python -m needs a module")
            if rest[1] not in ("pytest", "unittest"):
                raise SandboxRefusal(f"python -m {rest[1]!r} is not allowed (only pytest/unittest)")
        elif rest[:1] == ["-c"]:
            pass  # inline code is allowed; it still runs pinned to the sandbox
        elif rest and not rest[0].endswith(".py"):
            raise SandboxRefusal("python3 only runs .py scripts inside the repo (or -m pytest/-m unittest)")
    for arg in rest:
        _check_arg(arg)
    return [sys.executable if program in ("python3", "python") else program, *rest]


class Sandbox:
    """The strict execution environment: cwd pinned to the repo root,
    allowlisted commands only, writes only inside the repo."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        if not self.root.is_dir():
            raise WorkflowError(f"sandbox {root} does not exist")

    # -- commands -----------------------------------------------------------
    def run_command(self, command: str) -> dict[str, Any]:
        """Run one validated command. Returns {ok, rc, output, refused, ms}."""
        started = time.monotonic()
        try:
            argv = validate_command(command)
        except SandboxRefusal as exc:
            return {"ok": False, "rc": None, "output": f"REFUSED: {exc}", "refused": True, "ms": 0}
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.root),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONDONTWRITE": "1",
            "GIT_AUTHOR_NAME": "gx-coding-bench",
            "GIT_AUTHOR_EMAIL": "bench@gx.local",
            "GIT_COMMITTER_NAME": "gx-coding-bench",
            "GIT_COMMITTER_EMAIL": "bench@gx.local",
            "LANG": "C.UTF-8",
        }
        try:
            proc = subprocess.run(
                argv, cwd=str(self.root), env=env, capture_output=True,
                text=True, timeout=COMMAND_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "rc": None, "output": f"TIMEOUT after {COMMAND_TIMEOUT_S:.0f}s",
                    "refused": False, "ms": int((time.monotonic() - started) * 1000)}
        except FileNotFoundError:
            return {"ok": False, "rc": None, "output": "REFUSED: program not available in the sandbox",
                    "refused": True, "ms": 0}
        output = (proc.stdout + ("\n" + proc.stderr if proc.stderr.strip() else "")).strip()
        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS] + f"\n... (truncated, {len(output)} chars total)"
        return {
            "ok": proc.returncode == 0,
            "rc": proc.returncode,
            "output": output or "(no output)",
            "refused": False,
            "ms": int((time.monotonic() - started) * 1000),
        }

    # -- file writes ----------------------------------------------------------
    def write_file(self, rel_path: str, content: str) -> dict[str, Any]:
        """Write a file inside the sandbox. Paths are validated strictly."""
        rel_path = rel_path.strip().strip('"')
        _check_arg(rel_path)
        target = (self.root / rel_path).resolve()
        root_str = str(self.root)
        if not str(target).startswith(root_str + os.sep):
            raise SandboxRefusal(f"path escapes the sandbox: {rel_path}")
        # refuse if an existing path component is a symlink (escape vector)
        probe = self.root
        for part in Path(rel_path).parts:
            probe = probe / part
            if probe.is_symlink():
                raise SandboxRefusal(f"symlink in path: {part}")
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text(content, encoding="utf-8")
        return {"ok": True, "output": f"wrote {rel_path} ({len(content)} bytes)", "refused": False, "ms": 0}


# ---------------------------------------------------------------------------
# Parsing model output into tool blocks
# ---------------------------------------------------------------------------

_BASH_BLOCK = re.compile(r"```bash\s*\n(.*?)```", re.DOTALL)
_WRITE_BLOCK = re.compile(r"```write\s+([^\n]+)\n(.*?)```", re.DOTALL)


def extract_tool_blocks(text: str) -> tuple[list[str], list[tuple[str, str]], str]:
    """Split a model reply into (commands, writes, remainder prose).

    Commands: every ```bash block, one command per block (a block with
    several lines is split so each line is validated on its own -- the
    sandbox still refuses metacharacters per line).
    Writes: ```write <path> ... blocks.
    """
    commands: list[str] = []
    writes: list[tuple[str, str]] = []
    remainder = _BASH_BLOCK.sub("", text)
    remainder = _WRITE_BLOCK.sub("", remainder)
    for match in _BASH_BLOCK.finditer(text):
        block = match.group(1)
        for line in block.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                commands.append(line)
    for match in _WRITE_BLOCK.finditer(text):
        writes.append((match.group(1).strip(), match.group(2)))
    return commands, writes, remainder.strip()


# ---------------------------------------------------------------------------
# LLM calls through the gateway
# ---------------------------------------------------------------------------


def _gateway_key() -> str:
    return (os.environ.get("GX_GATEWAY_KEY") or os.environ.get("LITELLM_MASTER_KEY") or "").strip()


def llm_call(
    *,
    messages: list[dict[str, str]],
    agent: str,
    task: str,
    priority: str = "normal-worker",
    timeout: float = REQUEST_TIMEOUT_S,
) -> dict[str, Any]:
    """One STREAMING chat completion through the gateway with attribution
    headers. Streaming so per-call queue wait is measurable: the time to the
    first SSE byte contains scheduler wait + connection setup (same proxy
    as ops/bench/run_bench.py; prefill happens before the first content
    token but after the first byte).

    Retries bounded (HTTP_RETRIES, exponential backoff). Raises
    WorkflowError when the gateway stays unreachable or returns garbage.
    """
    key = _gateway_key()
    if not key:
        raise WorkflowError("no gateway key set (export GX_GATEWAY_KEY or LITELLM_MASTER_KEY)")
    body = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": 4096,
        "temperature": 0.2,
        "stream": True,
        "stream_options": {"include_usage": True},
        "chat_template_kwargs": {"enable_thinking": False},
    }
    data = json.dumps(body).encode()
    last_error = ""
    attempt = 0
    for attempt in range(HTTP_RETRIES):
        started = time.monotonic()
        req = urllib.request.Request(
            f"{GATEWAY_BASE}/v1/chat/completions", data=data, method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
                "X-GX-Project": PROJECT_HEADER,
                "X-GX-Agent": agent,
                "X-GX-Task": task,
                "X-GX-Priority": priority,
            },
        )
        content_parts: list[str] = []
        usage: dict[str, Any] = {}
        ttfb: float | None = None
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                for raw in resp:
                    if ttfb is None and raw.strip().startswith(b"data:"):
                        ttfb = time.monotonic()
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if event.get("usage"):
                        usage = event["usage"]
                    choices = event.get("choices") or []
                    if choices:
                        delta = choices[0].get("delta") or {}
                        if delta.get("content"):
                            content_parts.append(delta["content"])
            if not content_parts:
                raise WorkflowError("gateway returned an empty completion")
            return {
                "content": "".join(content_parts),
                "usage": usage,
                "queue_wait_ms": int((ttfb - started) * 1000) if ttfb is not None else None,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "retries": attempt,
            }
        except urllib.error.HTTPError as exc:
            detail = exc.read(200).decode("utf-8", "replace").strip()
            last_error = f"HTTP {exc.code}: {detail}"
            if exc.code in (400, 401, 403, 404):
                break  # not retryable
        except WorkflowError:
            raise
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
        time.sleep(min(2 ** attempt, 8))
    raise WorkflowError(f"gateway call failed after {attempt + 1} attempt(s): {last_error}")


# ---------------------------------------------------------------------------
# Concurrency observation (scheduler poll, best effort)
# ---------------------------------------------------------------------------


def observe_concurrency() -> int | None:
    """Active inference count from the orchestrator scheduler, best effort."""
    try:
        with urllib.request.urlopen(f"{ORCHESTRATOR_BASE}/scheduler/status", timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if isinstance(data, dict):
            active = data.get("active")
            return active if isinstance(active, int) else None
    except Exception:  # noqa: BLE001
        pass
    return None


class ConcurrencyObserver(threading.Thread):
    """Polls the scheduler in the background; keeps the max active count."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.max_active: int | None = None
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.wait(5):
            active = observe_concurrency()
            if active is not None and (self.max_active is None or active > self.max_active):
                self.max_active = active

    def stop(self) -> int | None:
        self._stop.set()
        self.join(timeout=3)
        return self.max_active


# ---------------------------------------------------------------------------
# Role phases
# ---------------------------------------------------------------------------


def render_template(name: str, **vars: str) -> str:
    """Render templates/<name> with <<var>> replacement (Jinja-free, no
    brace conflicts with code)."""
    template = (TEMPLATES / f"{name}.txt").read_text(encoding="utf-8")
    protocol = (TEMPLATES / "_protocol.txt").read_text(encoding="utf-8")
    for key, value in vars.items():
        template = template.replace(f"<<{key}>>", value)
    template = template.replace("<<protocol>>", protocol)
    leftover = re.findall(r"<<[a-z_]+>>", template)
    if leftover:
        raise WorkflowError(f"template {name} has unfilled placeholders: {leftover}")
    return template


def run_tool_loop(
    sandbox: Sandbox,
    role: str,
    prompt: str,
    metrics: dict[str, Any],
    *,
    task_id: str,
    max_rounds: int = MAX_TOOL_ROUNDS,
) -> str:
    """One role phase: LLM call(s) with the tool loop.

    The model's proposed commands/writes execute in the sandbox; outputs
    are fed back. Ends when the model produces no tool blocks or the round
    cap is hit. Returns the final reply text.
    """
    conversation: list[dict[str, str]] = [{"role": "user", "content": prompt}]
    final_text = ""
    for round_no in range(max_rounds):
        metrics["llm_calls"] += 1
        try:
            reply = llm_call(messages=conversation, agent=role, task=task_id)
        except WorkflowError as exc:
            metrics["errors"].append(f"{role}: {exc}")
            raise
        usage = reply["usage"]
        metrics["total_prompt_tokens"] += usage.get("prompt_tokens") or 0
        metrics["total_completion_tokens"] += usage.get("completion_tokens") or 0
        metrics["calls"].append({
            "role": role,
            "ts": time.strftime("%H:%M:%S"),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "queue_wait_ms": reply.get("queue_wait_ms"),
            "latency_ms": reply["latency_ms"],
            "retries": reply["retries"],
        })
        metrics["retries"] += reply["retries"]
        text = reply["content"]
        commands, writes, _ = extract_tool_blocks(text)
        if not commands and not writes:
            final_text = text
            break
        results: list[str] = []
        for rel_path, content in writes:
            try:
                out = sandbox.write_file(rel_path, content)
            except SandboxRefusal as exc:
                out = {"ok": False, "output": f"REFUSED: {exc}", "ms": 0}
            metrics["tool_time_ms"] += out["ms"]
            metrics["tool_ops"] += 1
            results.append(f"[write {rel_path}] {'ok' if out['ok'] else 'FAILED'}\n{out['output']}")
        for command in commands:
            out = sandbox.run_command(command)
            metrics["tool_time_ms"] += out["ms"]
            metrics["tool_ops"] += 1
            if out.get("refused"):
                metrics["refused_ops"] += 1
            results.append(f"[{command}] rc={out['rc'] if not out.get('refused') else 'refused'}\n{out['output']}")
        feedback = "\n\n".join(results)
        metrics["tool_outputs"].append({"role": role, "round": round_no, "feedback": feedback[:2000]})
        conversation.append({"role": "assistant", "content": text})
        conversation.append({"role": "user", "content": "TOOL RESULTS:\n" + feedback + "\n\nContinue. Remember the output format your role requires."})
        # keep the conversation bounded
        _trim(conversation)
        final_text = text
    return final_text


def _trim(conversation: list[dict[str, str]], limit: int = MAX_CONVERSATION_CHARS) -> None:
    total = sum(len(m["content"]) for m in conversation)
    while total > limit and len(conversation) > 2:
        removed = conversation.pop(1)  # keep the original task prompt
        total -= len(removed["content"])


_VERDICT_RE = re.compile(r"(?:VERDICT|FINAL):\s*(PASS|FAIL)", re.IGNORECASE)
_TEST_SUMMARY_RE = re.compile(r"(\d+)\s+(?:passed|ok)", re.IGNORECASE)
_TEST_FAIL_RE = re.compile(r"(\d+)\s+(?:failed|failure|failures|error|errors)", re.IGNORECASE)


def parse_verdict(text: str) -> str | None:
    match = _VERDICT_RE.search(text)
    return match.group(1).upper() if match else None


def parse_test_summary(texts: list[str]) -> dict[str, Any]:
    """Aggregate pass/fail counts from tool outputs (unittest/pytest style)."""
    passed = failed = 0
    for text in texts:
        for m in _TEST_SUMMARY_RE.finditer(text):
            passed = max(passed, int(m.group(1)))
        for m in _TEST_FAIL_RE.finditer(text):
            failed = max(failed, int(m.group(1)))
    return {"passed": passed, "failed": failed, "green": failed == 0 and passed > 0}


def run_round(
    *,
    sandbox_root: Path,
    label: str,
    round_no: int,
    observer: ConcurrencyObserver,
) -> dict[str, Any]:
    """One full workflow round against one sandbox. Returns the round report."""
    task_id = f"{label}-r{round_no}"
    started = time.monotonic()
    metrics: dict[str, Any] = {
        "llm_calls": 0,
        "total_prompt_tokens": 0,
        "total_completion_tokens": 0,
        "calls": [],
        "tool_time_ms": 0,
        "tool_ops": 0,
        "refused_ops": 0,
        "retries": 0,
        "errors": [],
        "tool_outputs": [],
    }
    sandbox = Sandbox(sandbox_root)
    phase_texts: dict[str, str] = {}

    def phase(role: str, prompt: str) -> str:
        try:
            text = run_tool_loop(sandbox, role, prompt, metrics, task_id=task_id)
        except WorkflowError:
            raise
        phase_texts[role] = text
        return text

    repo = str(sandbox_root)

    # 1. orchestrator: plan (no tools expected)
    phase("orchestrator", render_template("orchestrator", repo_path=repo))
    # 2. inspector
    findings = phase("inspector", render_template("inspector", repo_path=repo))
    findings_line = findings[-1500:]
    # 3. architect
    plan = phase("architect", render_template("architect", repo_path=repo, inspector_findings=findings_line))
    # 4. implementer
    implemented = phase("implementer", render_template(
        "implementer", repo_path=repo, architect_plan=plan[-1500:]))
    # 5. tester
    tested = phase("tester", render_template(
        "tester", repo_path=repo,
        implemented_files=re.search(r"IMPLEMENTED:\s*(.*)", implemented, re.IGNORECASE).group(1)
        if re.search(r"IMPLEMENTED:\s*(.*)", implemented, re.IGNORECASE) else "(unknown)"))
    # 6. reviewer
    review = phase("reviewer", render_template("reviewer", repo_path=repo))
    verdict = parse_verdict(review)
    # 7. repair only when the reviewer failed it
    if verdict == "FAIL":
        defects = review[-1500:]
        phase("repair", render_template("repair", repo_path=repo, review_defects=defects))
        review2 = phase("reviewer", render_template("reviewer", repo_path=repo))
        verdict = parse_verdict(review2) or verdict
    # 8. final validator
    validation = phase("validator", render_template("validator", repo_path=repo))
    final = parse_verdict(validation)

    test_results = parse_test_summary(
        [o["feedback"] for o in metrics["tool_outputs"]] + list(phase_texts.values())
    )
    wall_ms = int((time.monotonic() - started) * 1000)
    return {
        "round": round_no,
        "sandbox": str(sandbox_root),
        "label": label,
        "task_id": task_id,
        "wall_ms": wall_ms,
        "llm_calls": metrics["llm_calls"],
        "prompt_tokens": metrics["total_prompt_tokens"],
        "completion_tokens": metrics["total_completion_tokens"],
        "queue_wait_ms": [c.get("queue_wait_ms") for c in metrics["calls"]],
        "concurrency_observed": observer.max_active,
        "tool_time_ms": metrics["tool_time_ms"],
        "tool_ops": metrics["tool_ops"],
        "refused_ops": metrics["refused_ops"],
        "retries": metrics["retries"],
        "errors": metrics["errors"],
        "test_results": test_results,
        "reviewer_verdict": verdict,
        "final_verdict": final,
        "phases": {role: text[:2000] for role, text in phase_texts.items()},
        "metrics": metrics,
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def seed_repo(target: Path) -> None:
    """Run seed_repo.sh to create (or deterministically refresh) the repo."""
    proc = subprocess.run(["bash", str(SEED_SCRIPT), str(target)], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise WorkflowError(f"seed_repo.sh failed: {proc.stderr.strip()[:300]}")
    if not target.is_dir():
        raise WorkflowError(f"seed_repo.sh did not create {target}")


def print_round_summary(report: dict[str, Any]) -> None:
    print(f"\nround {report['round']} -- {report['sandbox']}")
    print(f"  wall time      {report['wall_ms'] / 1000:.1f} s")
    print(f"  LLM calls      {report['llm_calls']}  "
          f"(tokens: {report['prompt_tokens']} prompt + {report['completion_tokens']} completion)")
    print(f"  tool ops       {report['tool_ops']} ({report['refused_ops']} refused), "
          f"{report['tool_time_ms'] / 1000:.1f} s tool time")
    print(f"  retries        {report['retries']}   errors: {len(report['errors'])}")
    print(f"  tests          {report['test_results']['passed']} passed, {report['test_results']['failed']} failed"
          f" -> {'green' if report['test_results']['green'] else 'NOT green'}")
    print(f"  reviewer       {report['reviewer_verdict']}")
    print(f"  final verdict  {report['final_verdict']}")
    if report["concurrency_observed"] is not None:
        print(f"  max concurrent inference observed: {report['concurrency_observed']}")
    if report["errors"]:
        for err in report["errors"][:5]:
            print(f"  error: {err}")


def main(argv: list[str] | None = None) -> int:
    global GATEWAY_BASE  # noqa: PLW0603 -- the --gateway flag overrides it
    parser = argparse.ArgumentParser(
        prog="run_workflow",
        description="GX multi-agent coding benchmark (gx-auto through the gateway).",
    )
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--label", default="coding-bench")
    parser.add_argument("--sandbox", default=None,
                        help="reuse this repo dir instead of seeding a fresh one")
    parser.add_argument("--reseed", action="store_true",
                        help="re-seed the sandbox even when it exists")
    parser.add_argument("--gateway", default=GATEWAY_BASE)
    parser.add_argument("--report", default=None, help="explicit report JSON path")
    args = parser.parse_args(argv)

    GATEWAY_BASE = args.gateway.rstrip("/")

    if not _gateway_key():
        print("error: no gateway key set (export GX_GATEWAY_KEY or LITELLM_MASTER_KEY).")
        return 1
    if not (TEMPLATES / "orchestrator.txt").is_file():
        print(f"error: templates missing under {TEMPLATES}")
        return 1

    observer = ConcurrencyObserver()
    observer.start()
    reports: list[dict[str, Any]] = []
    try:
        for round_no in range(1, args.rounds + 1):
            if args.sandbox:
                sandbox_root = Path(args.sandbox).resolve()
                if args.reseed or not sandbox_root.is_dir():
                    seed_repo(sandbox_root)
            else:
                sandbox_root = Path(tempfile.mkdtemp(prefix=f"gx-coding-bench-r{round_no}-"))
                seed_repo(sandbox_root)
            print(f"round {round_no}/{args.rounds} -- sandbox {sandbox_root}")
            reports.append(run_round(
                sandbox_root=sandbox_root, label=args.label,
                round_no=round_no, observer=observer))
            print_round_summary(reports[-1])
    finally:
        concurrency = observer.stop()

    report_path = Path(args.report) if args.report else (
        REPORT_DIR / f"{time.strftime('%Y%m%dT%H%M%S')}-{args.label}.json"
    )
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "label": args.label,
            "rounds": args.rounds,
            "gateway": GATEWAY_BASE,
            "model": MODEL,
            "concurrency_observed": concurrency,
            "round_reports": reports,
        }, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"warn: cannot write report to {report_path}: {exc}", file=sys.stderr)

    print(f"\nreport: {report_path}")
    print("repo snapshots (kept): " + ", ".join(r["sandbox"] for r in reports))
    all_pass = all(r["final_verdict"] == "PASS" and r["test_results"]["green"] for r in reports)
    print("OVERALL: " + ("PASS" if all_pass else "FAIL"))
    return 0 if all_pass and reports else 1


if __name__ == "__main__":
    raise SystemExit(main())
