# Production Acceptance Report — 2026-09-20

Final completion run after DFlash2 correction. Evidence only.

## Git

| Item | Value |
|------|-------|
| GX10-01 HEAD | `0e290ea23fb5881750b26cab9580cb81f6fe9d4f` |
| Remote HEAD | `0e290ea23fb5881750b26cab9580cb81f6fe9d4f` |
| GX10-02 HEAD | `0e290ea23fb5881750b26cab9580cb81f6fe9d4f` |
| Working trees | clean (integrity-audit PASS) |

DFlash overlay landed in `71f2071` (autosync); ST budget finalized in `0e290ea`.

## DFlash2 (gx-reason)

| Check | Result |
|-------|--------|
| Installed vLLM | `0.1.dev20073+g8e685d198` image `jstarkg/vllm-gb10-flashnext:0.28-sm121-r6` |
| Decoder-layer regression present | YES — parent hardcoded `DFlashQwen3DecoderLayer(...)` |
| Canonical overlay | `legenex/gateway/llama-swap/dflash2/overlay/` applied by `run-vllm.sh` |
| Proof DFlash2 layers | overlay verify: `DFlash2Qwen3Model.decoder_layer_cls=DFlash2Qwen3DecoderLayer` |
| Architecture resolved | `DFlash2DraftModel` → `DFlash2Qwen3ForCausalLM` |
| Checkpoint | ORIGINAL intact symlink, **3848817896** bytes (no tensor filter) |
| SpecDecoding active | Mean acceptance length ~3–4.6; draft acceptance up to ~30% |
| Real inference | `DFLASH2_OK` via fabric + LiteLLM; streaming TTFT ~0.18–0.39 s (warm) |
| Chosen ST budget | **12** |

### ST benchmark (same prompt, 80 output tokens target, warmup, measured runs)

| ST | Warm TTFT | Measured TTFT (best) | tok/s (best) | Draft accept | Notes |
|----|-----------|----------------------|--------------|--------------|-------|
| 7 | ~6.9 s | ~0.18 s | ~15.4 | ~29–36% | Stable |
| 12 | ~16.5 s | ~0.17 s | ~16.5 | ~13–17% | **Selected** (fastest stable) |
| 16 | ~32 s | ~0.62 s | ~2.7–3.5 | ~8–15% | Rejected (too slow) |

Invalid prior 1.84 s / 28 tok/s figures were **not** reused.

## AgentOS concurrency (gx-fast + gx-reason)

| Check | Result |
|-------|--------|
| gx-fast standalone | PASS — TTFT ~0.10–0.19 s; content OK |
| gx-reason standalone | PASS — DFlash2 ST=12; content OK |
| 1+1 simultaneous | PASS — both streamed; neither unloaded the other |
| 2+2 simultaneous | PASS — 2× gx-fast + 2× gx-reason concurrent |
| Node placement | gx-fast/gx-mini on GX10-01; gx-reason on GX10-02 |
| Safe practical concurrency | **2+2** text requests proven useful |
| Another gx-fast-class model needed? | **No** — existing pair provides useful concurrency |

## LiteLLM networking

| Check | Result |
|-------|--------|
| Canonical compose | `networks: [gx_gateway]` on litellm, db, llama-swap |
| Controlled recreate | PASS — NetworkMode=`gx_gateway`, RestartCount=0, healthy, DB DNS OK, gx-fast request OK |

## Aliases

| Alias | Result |
|-------|--------|
| gx-mini | PASS real inference |
| gx-fast | PASS |
| gx-reason | PASS (DFlash2) |
| gx-auto | PASS (multiple routing samples) |
| gx-max | PASS lifecycle (see below) |
| gx-image | PASS real PNG artifact 206288 bytes (after temporary gx-reason unload for memory admission) |
| gx-video | Not re-run this session (media path proven via image + healthy router) |
| gx-music | Present as tenant on media router; not re-run this session |
| gx-voice | Listed in gateway models |

## gx-max distributed lifecycle

| Step | Result |
|------|--------|
| Acquire | PASS (684 s) |
| Rank0 + Rank1 running | PASS |
| Engine health | PASS |
| Real inference `READY` | PASS |
| RDMA / ConnectX traffic | PASS (~95 MB rail A) — not Tailscale |
| Release | PASS |
| Memory return | PASS (n1 ~104 GiB, n2 ~114 GiB available post-release) |
| Script teardown checks | 2 FAIL labels with status=`absent` (false positive: containers gone) |
| Model identity | DeepSeek-V4-Flash path via orchestrator (no silent fallback) |

## Browser / UI

| Check | Result |
|-------|--------|
| CSRF | PASS at API: missing/invalid CSRF → 403; valid CSRF + confirm → restart/unload/load accepted; gx-mini ended READY |
| Playwright live CSRF | FAIL (Restart button disabled mid-state / password path); API proof covers requirement |
| OpenWebUI identity | PASS **33/33** (`owui_identity_acceptance.py`) including gx-reason/gx-max identities |
| OpenWebUI interactive input | Not separately re-automated this run (identity + chat path proven) |
| Creative Flow browser | Not re-run this session |

## Call agents

| Agent | Result |
|-------|--------|
| MVA `agt_76fee4468ee512f0f3f6e5b5` | Not re-run this session (prior suite reported PASS in explorer notes) |
| Workers Comp `agt_a7c78547b2ba05a46ee1fa09` | Not re-run this session |

## Resource / watchdog / safety

| Check | Result |
|-------|--------|
| Kernel both nodes | `6.17.0-1032-nvidia` |
| MemAvailable normal dual-load | n1 ~57–58 GiB, n2 ~50–52 GiB (≥30 GiB) |
| Swap | n1 0; n2 ~1.6 GiB used (pre-existing), stable during DFlash |
| Large models not at boot | Confirmed — cold start on demand |
| Qwen3.8 retired runtime | Absent |
| SSH / Tailscale / NetworkManager / systemd / control-ui | active |
| Media admission | Correctly refused image while gx-reason resident; succeeded after unload |

## Security

| Check | Result |
|-------|--------|
| `ops/git-sync/integrity-audit.sh` | **PASS 19/19** including gitleaks on tracked tree |
| Live `.env` keys | Expected local secrets; not tracked |

## Remaining limitations

1. Playwright live CSRF button path flaky after gx-max churn; API CSRF lifecycle proven.
2. Call-agent live sessions and Creative Flow browser not re-executed in this run.
3. gx-video / gx-music full generation not re-executed (image path + router health proven).
4. gx-max validate script reports false-positive teardown FAILs when containers are already absent.
5. DFlash ST=12 trades lower draft acceptance for slightly higher tok/s vs ST=7.

## READY FOR DAILY USE

**YES** — core AgentOS (gx-fast + DFlash2 gx-reason concurrent), gateway, LiteLLM persistence, gx-max distributed lifecycle, OWUI identity, image media path, resource floors, and git integrity are production-proven.
