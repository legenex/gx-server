#!/usr/bin/env python3
"""Run the six-task Ornith gx-code coding suite against real workers.

Copies each fixture into a workdir, asks gx-code-01 or gx-code-02 to patch
it, applies FILE blocks from the model, runs pytest, and optionally gives
the model one repair round. Never edits files except to apply model output.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "work"
RESULTS = ROOT / "results"
GATEWAY = os.environ.get("GX_GATEWAY", "http://127.0.0.1:4000/v1")
KEY_FILE = Path("/home/legenex/Documents/Projects/Server/gx-cluster/legenex/gateway/.env")
MODEL_NAME = "Ornith-1.5-35B-A3B-Uncensored"
QUANT = "Q5_K_M"
RUNTIME = "llama.cpp"


def load_key() -> str:
    env = os.environ.get("LITELLM_MASTER_KEY") or os.environ.get("GX10_GATEWAY_KEY")
    if env:
        return env
    for line in KEY_FILE.read_text().splitlines():
        if line.startswith("LITELLM_MASTER_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("missing LiteLLM key")


KEY = load_key()

TASKS = [
    {
        "id": "1",
        "name": "diagnose and repair a bug",
        "src": "task1_bug",
        "model": "gx-code-01",
        "node": "GX10-01",
        "prompt": """You are repairing a Python bug in a small library. Marker GXSUITE-T1.

The function median() is wrong: it does not sort, and even-length lists must
return the average of the two central elements after sorting. clamp() is fine.

Return ONLY the full corrected stats.py using this format:

### FILE: stats.py
```python
...complete file...
```

Do not write tests. Do not touch other files. Do not invent APIs.""",
    },
    {
        "id": "2",
        "name": "frontend/UI following conventions",
        "src": "task2_frontend",
        "model": "gx-code-02",
        "node": "GX10-02",
        "prompt": """Implement a status pill in this tiny GX Widget UI. Marker GXSUITE-T2.

Conventions already in the project (do not invent a new design system):
- CSS classes prefixed gx-
- BEM modifiers with -- (gx-status-pill--ok / gx-status-pill--bad)
- data-testid attributes
- no inline styles
- no Bootstrap, no MUI

Requirements:
1. In index.html, inside the existing <header class="gx-header">, add
   <span class="gx-status-pill gx-status-pill--ok" data-testid="status-pill">online</span>
2. In app.js, on load, if the pill exists, keep text "online" and class
   gx-status-pill--ok. The refresh button behaviour must remain.

Return the files you change as:

### FILE: index.html
```html
...complete file...
```
### FILE: app.js
```javascript
...complete file...
```

Do not add new CSS frameworks. Do not rewrite styles.css unless required.""",
    },
    {
        "id": "3",
        "name": "backend/API implementation",
        "src": "task3_backend",
        "model": "gx-code-01",
        "node": "GX10-01",
        "prompt": """Implement create_item in api.py. Marker GXSUITE-T3.

Contract:
- create_item(name: str, qty: int) -> dict
- name must be a non-empty string after strip, else ValueError
- qty must be int >= 1, else ValueError
- store {"id", "name", "qty"} with incremental ids starting at 1
- keep list_items, get_item, reset working

Return ONLY:

### FILE: api.py
```python
...complete file...
```
""",
    },
    {
        "id": "4",
        "name": "multi-file refactor",
        "src": "task4_refactor",
        "model": "gx-code-02",
        "node": "GX10-02",
        "prompt": """Refactor duplicated tax math. Marker GXSUITE-T4.

user.py and order.py both define TAX_RATE and apply_tax. Extract a new
module tax.py with TAX_RATE = 0.15 and apply_tax(amount) -> round(amount * 1.15, 2).
user.py and order.py must import from tax and must NOT define TAX_RATE.
Keep user_total and order_total behaviour identical.

Return:

### FILE: tax.py
```python
...complete file...
```
### FILE: user.py
```python
...complete file...
```
### FILE: order.py
```python
...complete file...
```

Do not change tests. Do not add unrelated files.""",
    },
    {
        "id": "5",
        "name": "diagnose and repair an integration failure",
        "src": "task5_integration",
        "model": "gx-code-01",
        "node": "GX10-01",
        "prompt": """Fix the producer/consumer integration. Marker GXSUITE-T5.

consumer.handle(event) requires event["user_id"] (snake_case). producer.make_event
currently emits userId. Tests fail on the round trip.

Fix the producer to emit {"user_id": ..., "action": ...}. Do not change the
consumer contract.

Return:

### FILE: producer.py
```python
...complete file...
```
""",
    },
    {
        "id": "6",
        "name": "autonomous inspect-implement-test-repair-retest",
        "src": "task6_autonomous",
        "model": "gx-code-02",
        "node": "GX10-02",
        "prompt": """You are working autonomously in tinycalc. Marker GXSUITE-T6.

Inspect README.md and ops.py. Implement the missing/incorrect functions so
test_ops.py would pass:

- div(a, b) true division, ZeroDivisionError if b == 0
- power(a, b)
- mean(xs) arithmetic mean, ValueError on empty

Keep add/sub/mul.

Return:

### FILE: ops.py
```python
...complete file...
```
""",
    },
]


FILE_RE = re.compile(
    r"### FILE:\s*(\S+)\s*\n```(?:[a-zA-Z0-9_+-]+)?\n(.*?)```",
    re.S,
)


def chat(model: str, prompt: str, timeout: int = 240) -> dict:
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a careful software engineer. Output complete files "
                        "in ### FILE: path fenced blocks. No extra commentary after the files."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 2048,
            "stream": False,
        }
    ).encode()
    req = urllib.request.Request(
        GATEWAY.rstrip("/") + "/chat/completions",
        data=body,
        headers={
            "Authorization": "Bearer " + KEY,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    elapsed = time.time() - t0
    data = json.loads(raw.decode())
    content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    usage = data.get("usage") or {}
    comp = int(usage.get("completion_tokens") or 0)
    tps = (comp / elapsed) if elapsed > 0 and comp else None
    return {
        "content": content,
        "usage": usage,
        "elapsed": elapsed,
        "tps": tps,
        "model_served": data.get("model"),
        "raw_id": data.get("id"),
    }


def apply_files(workdir: Path, content: str) -> list[str]:
    changed = []
    for rel, body in FILE_RE.findall(content):
        rel = rel.strip().lstrip("./")
        if ".." in rel or rel.startswith("/"):
            continue
        dest = workdir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(body if body.endswith("\n") else body + "\n")
        changed.append(rel)
    return changed


def run_pytest(workdir: Path) -> dict:
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=short"],
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=60,
    )
    out = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
    harness_fail = "No module named pytest" in out or r.returncode == 2 and "usage:" in out.lower()
    return {
        "rc": r.returncode,
        "passed": r.returncode == 0,
        "stdout": out[-4000:],
        "stderr": (r.stderr or "")[-2000:],
        "harness_fail": harness_fail,
    }


def copy_src(src: str) -> Path:
    dest = WORK / src
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(ROOT / src, dest)
    return dest


def score(first_pass: bool, final_pass: bool, changed: list[str], expected_hint: str, content: str) -> str:
    if not changed:
        return "FAIL"
    halluc = bool(re.search(r"\b(fastapi|django|express|react|vue)\b", content, re.I)) and "fastapi" not in expected_hint
    if final_pass and first_pass:
        return "PASS"
    if final_pass and not first_pass:
        return "PARTIAL"
    if halluc:
        return "FAIL"
    return "FAIL"


def main() -> int:
    WORK.mkdir(exist_ok=True)
    RESULTS.mkdir(exist_ok=True)
    reports = []
    for task in TASKS:
        print(f"\n===== TASK {task['id']} {task['name']} via {task['model']} =====", flush=True)
        workdir = copy_src(task["src"])
        listing = []
        for p in sorted(workdir.rglob("*")):
            if p.is_file() and p.name != "__pycache__":
                listing.append(f"----- {p.relative_to(workdir)} -----\n{p.read_text()}")
        prompt = task["prompt"] + "\n\nCurrent files:\n" + "\n\n".join(listing)
        try:
            resp = chat(task["model"], prompt)
        except Exception as exc:  # noqa: BLE001
            rec = {
                "task": task["name"],
                "id": task["id"],
                "repo": str(workdir),
                "node": task["node"],
                "model": task["model"],
                "quant": QUANT,
                "runtime": RUNTIME,
                "error": str(exc),
                "verdict": "FAIL",
            }
            reports.append(rec)
            print("ERROR", exc, flush=True)
            continue
        changed = apply_files(workdir, resp["content"])
        testr = run_pytest(workdir)
        first = testr["passed"]
        repair_used = False
        if testr.get("harness_fail"):
            raise SystemExit(f"pytest harness failed in {workdir}: {testr['stdout']}")
        if not first:
            repair_used = True
            repair_prompt = (
                task["prompt"]
                + "\n\nThe tests failed. Repair using the same FILE format.\n"
                + testr["stdout"]
                + testr["stderr"]
            )
            try:
                resp2 = chat(task["model"], repair_prompt)
                extra = apply_files(workdir, resp2["content"])
                changed = list(dict.fromkeys(changed + extra))
                testr = run_pytest(workdir)
                resp["repair_elapsed"] = resp2["elapsed"]
                resp["repair_tps"] = resp2["tps"]
                resp["repair_usage"] = resp2["usage"]
                resp["content"] += "\n\n--- REPAIR ---\n" + resp2["content"]
            except Exception as exc:  # noqa: BLE001
                testr["stderr"] += f"\nrepair error: {exc}"
        verdict = score(first, testr["passed"], changed, task["src"], resp["content"])
        unnecessary = [c for c in changed if c.startswith("test_")]
        rec = {
            "task": task["name"],
            "id": task["id"],
            "repo": f"tests/ornith-suite/{task['src']}",
            "node": task["node"],
            "model": f"{task['model']} / {MODEL_NAME}",
            "quant": QUANT,
            "runtime": RUNTIME,
            "time_s": round(resp["elapsed"], 2),
            "tokens_per_sec": round(resp["tps"], 2) if resp["tps"] else None,
            "usage": resp["usage"],
            "files_changed": changed,
            "tests_run": "pytest -q",
            "tests_passed": testr["passed"],
            "first_attempt_passed": first,
            "repair_round": repair_used,
            "human_rescue_required": not testr["passed"],
            "edited_unnecessary_files": bool(unnecessary),
            "hallucinated_apis": bool(re.search(r"fastapi|django|flask.Flask", resp["content"]))
            and task["id"] != "3",
            "claimed_success_incorrectly": False,
            "verdict": verdict,
            "pytest_stdout": testr["stdout"][-1500:],
            "served": resp.get("model_served"),
        }
        reports.append(rec)
        (RESULTS / f"task{task['id']}.md").write_text(
            json.dumps(rec, indent=2) + "\n\nMODEL OUTPUT:\n" + resp["content"][:8000]
        )
        print(
            f"verdict={verdict} tests={testr['passed']} files={changed} "
            f"t={resp['elapsed']:.1f}s tps={resp['tps']}",
            flush=True,
        )
        print(testr["stdout"][-400:], flush=True)

    passed = sum(1 for r in reports if r.get("verdict") == "PASS")
    partial = sum(1 for r in reports if r.get("verdict") == "PARTIAL")
    failed = sum(1 for r in reports if r.get("verdict") == "FAIL")
    summary = {
        "model": MODEL_NAME,
        "quant": QUANT,
        "runtime": RUNTIME,
        "pass": passed,
        "partial": partial,
        "fail": failed,
        "score": f"{passed}/6 PASS, {partial} PARTIAL, {failed} FAIL",
        "tasks": reports,
    }
    (RESULTS / "summary.json").write_text(json.dumps(summary, indent=2))
    print("\nOVERALL", summary["score"], flush=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
