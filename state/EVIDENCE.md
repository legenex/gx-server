# EVIDENCE

## Connectivity (2026-09-23)

- gx10-01 hostname `gx10-01`, Tailscale `100.105.214.61`
- gx10-02 Tailscale `100.73.238.4`, SSH `Host gx10-02` as `legenex-02`
- RoCE A `192.168.100.10` ↔ `192.168.100.11`
- RoCE B `192.168.101.10` ↔ `192.168.101.11`
- `gx doctor` 13/13

## Backup

- Stamp `20260923T091634Z` (UI Back Up Now)
- Local gx10-01 `24ba5393` 22.167 GiB
- Local gx10-02 `fa6b8856` 9.235 GiB
- Peer copies `f5588ad0` / `bbee85a2`
- Prior verified stamp `20260923T090136Z` (`4ebe7225` / `a35af18c`) kept
- `restic check --read-data-subset=5%` no errors; UI Verify 2% no errors
- Restore `models.lock` sha256 `b70269f6…56f65` match
- Password files SHA-256 identical on both nodes; mode 0600
- GX10-02 disk: unused Docker images pruned (~102 GB) so copy could land; llama-cpp `:latest` retagged

## Coding suite

See `docs/BENCHMARKS.md`. Results JSON: `tests/ornith-suite/results/summary.json`. **5/6 PASS, 1 PARTIAL.**

## Concurrency

- gx-code-01 1.85 s 51 t/s NODEONE-PROOF
- gx-code-02 2.10 s 52 t/s NODETWO-PROOF
- node2 llama.cpp log: eval 110 tokens, 57.53 t/s
- Post job `gx-code` LB0–LB3 0.16–0.21 s

## gx-auto

`/srv/logs/gx-auto-routing.jsonl`: greeting/summary/reason → gx-mini; scripts/coding/debug → gx-code. Busy coding: decision tier gx-code, 11890 ms, not mini.

## gx-max

`gx_max: {mode: dual-worker, solver: gx-code-01, reviewer: gx-code-02, verdict: fail}` then repair; 46.4 s. OpenWebUI gx-max 6.6 s with the same banner after merging system turns.

## OpenWebUI / AgentOS / UI

- OWUI picker four modes; 24 chats remain
- AgentOS `/api/ask-live` gx-code, concurrent pair both gx-code
- Playwright: all seven primary pages authenticated; Backup last snapshot `20260923T090136Z`
