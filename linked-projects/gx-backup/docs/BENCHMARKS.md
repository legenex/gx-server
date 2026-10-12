# Benchmarks

Measured 2026-09-23. Quality over peak tok/s.

## gx-mini (Ornith-1.5-9B Q8_0, llama.cpp, gx10-01)

- Files sha256-verified against Hugging Face LFS.
- Resident. `Pong` 0.35 s (3 completion tokens). Counting/short chat ~11 t/s historically; ping path is not the quality metric.

## gx-code (Ornith-1.5-35B-A3B Q5_K_M, llama.cpp, both nodes)

Independent workers. Thinking disabled via `--env-file /app/env/gx-code.env`.

| Worker | Ping | Concurrent coding | tok/s |
|---|---|---|---|
| gx-code-01 | NODEONE 0.22 s | 1.85 s factorial helper | 51 |
| gx-code-02 | NODETWO 0.27 s | 2.10 s factorial helper | 52 |

Load-balance on alias `gx-code` after concurrent jobs: four short `LB0`–`LB3` replies, 0.16–0.21 s, workers stable.

### Six-task real coding suite

Fixture: `tests/ornith-suite/` (copies, not production repos). Model Q5_K_M. pytest in a local venv.

| Task | Repo | Node | Model | Quant | Runtime | Time | tok/s | Files | Tests | Passed | Human rescue | Extra files | Hallucinated APIs | False success | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Diagnose/repair bug | task1_bug | GX10-01 | gx-code-01 Ornith | Q5_K_M | llama.cpp | 3.5 s | 45 | stats.py | pytest 4 | yes | no | no | no | no | PASS |
| Frontend/UI conventions | task2_frontend | GX10-02 | gx-code-02 Ornith | Q5_K_M | llama.cpp | 6.2 s | 56 | index.html, app.js | pytest 5 | yes | no | no | no | no | PASS |
| Backend/API | task3_backend | GX10-01 | gx-code-01 Ornith | Q5_K_M | llama.cpp | 6.1 s | 45 | api.py | pytest 3 | yes | no | no | no | no | PASS |
| Multi-file refactor | task4_refactor | GX10-02 | gx-code-02 Ornith | Q5_K_M | llama.cpp | 3.6 s | 55 | tax.py, user.py, order.py | pytest 3 | yes | no | no | no | no | PASS |
| Integration failure | task5_integration | GX10-01 | gx-code-01 Ornith | Q5_K_M | llama.cpp | 2.5 s | 43 | producer.py | pytest 2 | yes | no | no | no | no | PASS |
| Autonomous inspect→repair | task6_autonomous | GX10-02 | gx-code-02 Ornith | Q5_K_M | llama.cpp | 2.0 s + repair | 52 | ops.py | pytest 4 | yes after repair | no | no | no | no | PARTIAL |

**Overall coding reliability: 5/6 PASS, 1 PARTIAL, 0 FAIL.**

Task 6 first attempt omitted empty-list and divide-by-zero guards; the model’s own repair round added them and tests passed. Q4 was not compared: failures were incomplete edge handling, not a quant/runtime crash.

## gx-auto

Unit: `tests/test_routing.py` OK.

Live `/srv/logs/gx-auto-routing.jsonl`:

- greeting / summarisation / simple reasoning → `gx-mini`
- light script / substantial coding / difficult debugging → `gx-code`
- coding while both code workers occupied → still `gx-code` (11.9 s), **not** gx-mini

## gx-max

Dual-worker (`GX_MAX_MODE=dual-worker`). Real longest-substring bug, 46.4 s.

- solver: gx-code-01
- reviewer: gx-code-02, JSON verdict fail (off-by-one, start index, empty string)
- repair phase ran; final answer traces `abba`/`pwwkew`/`bbbbb` and includes `solver=` / `reviewer=` / `verdict=`

OpenWebUI gx-max (after merging duplicate system turns for the Ornith template): 6.6 s, banner `solver=gx-code-01 reviewer=gx-code-02`.
