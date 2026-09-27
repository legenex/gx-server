# ops/bench — reproducible V4.1 benchmark suite

Measures the DeepSeek V4.1 Flash EXL3 runtime (Mia 2× DGX Spark kit) exactly
as ARCHITECTURE-V41.md §9 asks: startup, TTFT, prefill/decode tps, aggregate
tps, memory low-water, queue behavior, and the multi-agent coding benchmark.

Everything is stdlib-only Python (`urllib` as the OpenAI client). Results
append as JSONL to `state/bench/results.jsonl` (one JSON object per line) and
a human summary table prints to stdout. `gx benchmark` (the CLI) is the
intended entry point; `run_bench.py` also runs standalone.

## Suites

### `--suite quick` (~1 minute)
| op | what it measures |
|---|---|
| `health` | GET `:8888/health` and `/v1/models` timing + served ids |
| `short_decode` | 1 stream, 64-token decode: TTFT + decode tok/s (streaming) |
| `smoke` | 17×19=323 completion check (the Mia smoke, MIA-RUNTIME.md) |
| `tool_call` | one `get_weather` function call; PASS when the model emits a parseable tool call (DSML parser health) |
| memory | MemAvailable sampled at 1 Hz for the whole run; low-water recorded |

### `--suite full` (quick + ~10–20 minutes depending on ladder)
Adds:
| op | what it measures |
|---|---|
| `context_{100,8000,32000,64000,128000}` | synthetic prose prompts at each size: prefill tok/s, TTFT |
| `context_*_repeat` | same prompt again: `prompt_tokens_details.cached_tokens` proves the prefix cache works |
| `concurrency` × 1, 2, 4 | parallel 64-token decodes: per-stream tps + aggregate tps |

### `--suite coding`
Delegates to `ops/bench/coding-bench/run_workflow.py` (its own README).

### `--suite matrix` (via `gx benchmark --suite matrix`)
Quick suite **per profile**, with an orchestrator restart between profiles —
see the procedure below.

## The PHASE-35 matrix procedure (speculation × streams × profile)

Speculation is a **server** setting (profiles), not a request flag, so the
matrix is *between suites*, not within one. The script measures whatever is
live and records the live profile on every line.

```bash
# one-off, whatever is live:
gx benchmark --suite quick --label adhoc          # via the CLI
python3 ops/bench/run_bench.py --suite quick --label adhoc

# the full matrix (restart per profile, quick suite on each):
gx benchmark --suite matrix
```

`gx benchmark --suite matrix` does, for each profile in the registry
(fast, balanced, swarm, deep, long, in that order):

1. `POST /lifecycle/gx-max/restart {"profile": P}` — boots that profile
   (~25 min cold; ~25 min again per restart — plan for hours, not minutes),
2. wait for READY,
3. `run_bench.py --suite quick --label matrix-<P>`.

Per the measured reference (MIA-RUNTIME.md): ×1 stream is fastest WITH
speculation (dspark, `fast`/`balanced`/`deep`), ×4 aggregate is fastest
WITHOUT it (`swarm`). The matrix makes that visible on this cluster's
current build. A finer matrix (e.g. full suite per profile) is the same
procedure with `--suite full`.

## Recorded metric line (results.jsonl)

One JSON object per line; `ts` first. Fields: `ts, label, suite, op, profile,
streams, prompt_tokens, completion_tokens, cached_tokens, ttft_ms,
prefill_tps, decode_tps, aggregate_tps, queue_wait_ms, total_ms, error`,
plus op-specific extras (`smoke_ok`, `tool_ok`, `target_tokens`, `repeat`,
`per_stream_tps`, `wall_ms`). A final `run_summary` line per run carries
`mem_start_gib`, `mem_low_water_gib`, GPU temp/power snapshot, and the
record count.

`queue_wait_ms` is a proxy: the time-to-first-byte of the streaming request,
which contains scheduler queue wait + connection setup (prefill happens
between the first byte and the first *content* token). It is NOT the
scheduler's own queue-time metric — the orchestrator history has that.

## What "good" looks like (vs the Mia reference, MIA-RUNTIME.md)

Stock weights, DSpark k=3, 2× GB10, TP=2:

| metric | reference | notes |
|---|---|---|
| smoke 17×19 | `323` | must PASS every time |
| decode ×1 | 31.6 tok/s, TTFT 221 ms | WITH speculation (fast/balanced/deep) |
| decode ×1, no spec | ~23 tok/s | swarm-style |
| ×2 aggregate | 42.5 tok/s (21.6/stream, TTFT 347 ms) | |
| ×4 aggregate | 42.8 tok/s WITH spec; 53.7 without | swarm wins at 4 streams |
| prefill 8k–128k | ~970–1055 tok/s | 872.6 at 256k; 995 median on 34k replay |
| cached repeat | `cached_tokens` ≈ prompt_tokens | prefix cache retention 4096 |
| MemAvailable after 34k replay | ≈ 5.9 GiB | low-water below ~2 GiB is a red flag |
| boot | ~25 min | health check should fail fast before boot |

A run that lands within ~10% of these numbers on the stock pack is healthy;
the uncensored candidate should be compared with `compare_stock.py`
(ops/uncensor-verify) and the same matrix.

## Degradation

No hangs: every HTTP call has a timeout (`GX_TIMEOUT_S`, default 6 s for
health, 300 s per streaming request, ladder cases scale with prompt size).
If the model endpoint is down the suite exits 1 immediately with the
message to run `gx start` first.
