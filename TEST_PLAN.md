# Acceptance test plan

The system is not complete until every item below is checked. Checked items
link to the evidence in `TEST_RESULTS.md`; do not check an item without a
real result recorded there — this file tracks intent, `TEST_RESULTS.md`
tracks evidence.

Last reviewed: 2026-09-15.

## 1. Infrastructure

- [x] both nodes on kernel `6.17.0-1032-nvidia` — reverified live 2026-09-15
- [x] LAN connectivity both ways
- [x] both ConnectX rails active — reverified live 2026-09-15
- [x] Tailscale management reachable
- [x] SSH both directions — reverified live 2026-09-15
- [x] NCCL two-node test passes (`TEST_RESULTS.md` §3)

## 2. gx-mini

- [x] real text inference (`TEST_RESULTS.md` §4)
- [x] real image/vision inference
- [x] lifecycle start (cold load via llama-swap)
- [x] health check
- [ ] lifecycle stop + memory-returns-to-baseline, captured as its own
  isolated measurement (implied by other tests, not recorded standalone)

## 3. gx-fast

- [x] real inference (`TEST_RESULTS.md` §4)
- [x] tool-use prompt
- [x] vision test
- [ ] lifecycle start/stop re-verified since the node-1 gateway/orchestrator
  incident — see `TASKS.md` P1
- [x] safe memory headroom maintained (verified statically; not cold-started
  in the most recent session)

## 4. gx-reason

- [x] **UNBLOCKED 2026-09-16 — B-011 closed.** The tier was re-engined onto
  `nvidia/Qwen3.6-27B-NVFP4` + vLLM (D-021). Difficult reasoning inference
  passes on GPU through the real gateway: the bat-and-ball problem answered
  correctly, `reasoning_content` separated, 12.4 tok/s. The original garbage
  repro prompt now returns " Paris." (`TEST_RESULTS.md` §14). Never routed to
  CPU — the "no accidental CPU execution" requirement is intact.
- [x] safe single-node fit — re-measured on the new engine: ~44 GiB real
  node-level footprint, 70 GiB still available with it loaded
- [x] lifecycle start/stop, safe memory headroom — cold start 401 s, unload
  returns memory to 116 GiB MemAvailable in ~5 s
- [x] vision input — verified live: a generated image of three blue circles
  sent through the gateway was described correctly as "3 blue"
  (`TEST_RESULTS.md` §14.4), so `supports_vision: true` is evidence-backed
- [ ] tool calling (`--tool-call-parser qwen3_xml`) — configured, not yet
  exercised with a real tool-call round trip

## 5. gx-max

- [x] both ranks launch (`TEST_RESULTS.md` §1, §5)
- [x] both ranks remain alive
- [x] SGLang health endpoint passes
- [x] real DeepSeek V4 Flash completion/chat inference
- [x] tokens/sec captured
- [x] ConnectX/NCCL path active, confirmed via RDMA hardware counters
- [x] stop both ranks cleanly
- [x] memory returns on both nodes
- [ ] **re-verify the full cycle since node 2's power cycle** — the passing
  run above predates the B-012 wedge; `legenex/tests/gx-max-validate.sh` has
  not been re-run against the recovered node 2 (`TASKS.md` P1)

## 6. gx-auto

- [x] simple task routes to mini
- [x] normal agent task routes to fast (tool-bearing request)
- [x] harder reasoning/coding routes to reason
- [x] extreme/high-value task can route to max
- [x] direct gx-max request never silently downgrades
- [x] routing decisions logged (JSON, `gx.routing` logger)

## 7. lifecycle transition

- [x] start a normal single-node model
- [x] request gx-max
- [x] verify queue/drain
- [x] verify conflicting model unload
- [x] verify memory headroom
- [x] start gx-max
- [x] complete inference
- [x] stop gx-max
- [x] verify memory returned
- [x] restore normal workloads
(Full scenario passed once, `TEST_RESULTS.md` §5 — see gx-max row above for
why it needs a repeat run post-recovery.)

## 8. image

- [ ] generate one actual image through the gx-image API/router — **NOT RUN**.
  ComfyUI is provisioned and reachable on node 2 (confirmed 2026-09-15); the
  router itself has 43 passing unit/protocol tests, but no real end-to-end
  generation has been recorded.
- [ ] verify output file
- [ ] verify model unload/reload behavior

## 9. video

- [ ] generate one short actual video through the gx-video API/router —
  **NOT RUN**, same caveat as gx-image.
- [ ] verify output file
- [ ] verify model unload/reload behavior

## 10. restart and recovery

- [ ] reboot a node with no large model auto-start, verify management
  services return first — **NOT RUN** as a deliberate reboot test (node 2's
  B-012 power cycle was a real-world instance of this, but not a controlled
  test)
- [x] gateway/lifecycle services recover after a container-level failure
  (verified live 2026-09-15: `gx-litellm` restarted cleanly after an
  administratively-terminated DB connection)
- [x] aliases recover (orchestrator correctly reports all tiers
  `stopped`/`usable` after the restart)
- [ ] watchdog behavior tested safely — hardware watchdog is deliberately
  unarmed (B-014); `gx-hostwatch.sh` logs/alerts only, not yet tested under
  a real fault it should catch
- [x] stale RDP session recovery documented (`OPERATIONS.md`)
- [ ] remote-access-over-Tailscale acceptance test — **NOT RUN**
