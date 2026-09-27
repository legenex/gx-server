# ops/bench/coding-bench — the multi-agent coding benchmark

Measures whether the cluster can actually ship a small feature-fix
end-to-end through the full gateway stack (gx-auto, scheduler attribution,
profiles) — not just tokens per second. ARCHITECTURE-V41.md §9 asks for a
"disposable repo multi-agent workflow (orchestrator → inspector →
architect → implementer(s) → tester → reviewer → repair → validator) with
full metrics"; this is it.

## Workflow

```
            ┌──────────────────────────────────────────────────────┐
            │  gateway (gx-auto)  ←  attribution headers on every  │
            │        │              call: X-GX-Project=gx-coding-   │
            │        ▼               bench, X-GX-Agent=<role>,      │
  seed_repo.sh ──► ORCHESTRATOR ──►  X-GX-Task=<id>, X-GX-Priority  │
   /tmp/gx-coding-    (plan)                                          │
     bench-<ts>/          │                                           │
   task-tracker           ▼                                           │
   + seeded bug       INSPECTOR ──(bash blocks)──► sandbox ──┐       │
                          │                                  │      │
                          ▼                                  │ tool │
                     ARCHITECT (plan)                        │ results
                          │                                  │ fed  │
                          ▼                                  │ back │
                     IMPLEMENTER ──(write blocks)────────────┤       │
                          │                                  │      │
                          ▼                                  │      │
                     TESTER ──(unittest/pytest)──────────────┘       │
                          │                                           │
                          ▼                                           │
                     REVIEWER ─── VERDICT: PASS ───┐                 │
                          │                        │                 │
                     VERDICT: FAIL                 ▼                 │
                          │                   VALIDATOR ── FINAL:    │
                          ▼                       PASS/FAIL           │
                      REPAIR ──► re-test ─────────────┘              │
            └──────────────────────────────────────────────────────┘
```

The seeded repo (`seed_repo.sh`, /tmp, no network) is a stdlib task-tracker
REST app with deliberate gaps: no input validation, no error handling, no
tests, and one deterministic seeded bug (`delete_task` removes the LAST task
whatever id you pass). The seeder is idempotent (`test_seed_repo.sh`).

## The harness contract

Roles act through a STRICT sandbox — the model only *proposes*:

- ```bash blocks: one command each. Allowlist: `pytest`, `python3` (in-repo
  scripts, `-m pytest`, `-m unittest`), `git status|diff|log|add|commit|show`.
  cwd is pinned to the sandbox; relative paths only; no `..`, no absolute
  paths, no shell metacharacters (`; | & > < \` $ \\`), no rm/curl/wget/sudo.
  Anything else is REFUSED and the refusal text is fed back so the model can
  retry.
- ```write <relative/path> blocks: full file contents, written only inside
  the sandbox (traversal and symlink escapes refused).

Enforcement is tested in `test_sandbox.py` (rm -rf, curl, sudo, cd escape,
traversal, semicolon chains all refused). Note: `pytest` itself may not be
installed on the head node — the harness reports it as unavailable and the
roles fall back to `python3 -m unittest discover -s tests` (also allowlisted).

## What success means

- the test suite the workflow wrote is GREEN (>0 passed, 0 failed),
- the final validator emits `FINAL: PASS`,
- the reviewer verdict is `VERDICT: PASS` (the repair agent runs only on
  FAIL, once),
- wall time is bounded (typically minutes on a booted cluster; the whole
  run fails fast with a clear message when the gateway/model is down),
- zero non-refused sandbox violations (refused proposals are counted and
  reported, not errors — models learn).

## Metrics recorded (report JSON)

Per round: wall time, LLM calls, prompt/completion tokens, per-call queue
wait (ttfb proxy, same definition as ops/bench/run_bench.py), inference
concurrency observed (background scheduler poll), tool time, tool ops,
refused ops, retries, errors, test results, reviewer verdict, final
verdict, per-phase transcripts (truncated).

Reports live under `state/bench/coding/` (LOCAL ONLY — never push raw
transcripts to the public repo).

## Usage

```bash
bash ops/bench/coding-bench/seed_repo.sh            # seed only
python3 ops/bench/coding-bench/run_workflow.py --rounds 1 --label ph35
gx benchmark --suite coding                          # via the CLI
python3 ops/bench/coding-bench/test_sandbox.py       # hermetic tests
bash  ops/bench/coding-bench/test_seed_repo.sh       # hermetic tests
```

Everything is restartable: `--sandbox DIR` reuses an existing repo;
`--reseed` re-runs the deterministic seeder. Sandbox snapshots are kept
after the run (their path is printed and recorded in the report).
