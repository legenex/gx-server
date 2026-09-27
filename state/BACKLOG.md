# Mission Backlog — GX10 DeepSeek V4.1 Flash Clean Rebuild

Status legend: [ ] pending, [~] in progress, [x] done, [!] blocked

## Track A — Protect + Inventory (PHASES 1-10)
- [x] Ground truth, gx-backup verify, ZIP checksums, pre-V4.1 tags pushed
- [~] PHASE 6 inventory: filesystem, systemd user units, Docker, models, storage (both nodes)
- [ ] PHASE 7 stop obsolete AI workloads (via orchestrator/resource-guard, not raw docker stop)
- [ ] PHASE 8 delete obsolete model weights/runtimes/services (post-evidence)
- [ ] PHASE 9 filesystem normalisation (state/filesystem-{before,migration,after}.json)
- [ ] PHASE 10 storage freed verification

## Track B — Model (PHASES 11-21)
- [ ] PHASE 11 ConnectX/RoCE validation (both rails, NCCL proof)
- [ ] PHASE 12 headless prerequisites (reversible, SSH verified first)
- [ ] PHASE 13 pin/fetch MiaAI runtime (https://github.com/MiaAI-Lab/DeepSeek-v4.1-Flash-EXL3-2x-DGX-Sparks) — commit, checkpoint revision, container digest
- [ ] PHASE 14 stage DeepSeek V4.1 EXL3 ~2.9bpw across both nodes
- [ ] PHASE 15-17 stock baseline: launch, chat/tool/vision tests, benchmark
- [ ] PHASE 18-19 uncensoring: abliterated overlay first (drowzeys/*), full dealignai checkpoint only if needed
- [ ] PHASE 20 production uncensored selection + verification suite
- [ ] PHASE 21 remove duplicate checkpoint data

## Track C — Platform (PHASES 22-34)
- [ ] PHASE 22 lifecycle controller (start/stop/drain/restart; no boot autoload)
- [ ] PHASE 23 admission scheduler (priorities, per-project limits, persistence)
- [ ] PHASE 24 LiteLLM integration (gx-max, gx-auto only)
- [ ] PHASE 25-26 gx-max + gx-auto (profiles FAST/BALANCED/SWARM/DEEP/LONG CONTEXT/CUSTOM, reasoning ladder)
- [ ] PHASE 27-31 GX Cluster Control Center rebuild (overview, controls, perf, requests, agents, tasks, projects, files, storage, logs, network, updates, settings, recovery)
- [ ] PHASE 32 AgentOS adapter
- [ ] PHASE 33 Hostinger KVM4 control plane (discover access; compose either way)
- [ ] PHASE 34 watchdog/recovery

## Track D — Prove + Close (PHASES 35-44)
- [ ] PHASE 35 performance matrix (1/2/4 streams × short/8K/32K/64K/128K, speculation on/off)
- [ ] PHASE 36 real autonomous coding benchmark (multi-agent, tests pass, reviewer verdict)
- [ ] PHASE 37-38 gx-backup update + non-destructive restore validation
- [ ] PHASE 39-41 independent reviews (infra/security/model/dashboard/multi-agent/recovery) + repairs + regression
- [ ] PHASE 42-44 final cleanup scan, commit/push, docs/FINAL-REPORT.md

## Fast-track deferrals (2026-09-27, post-Gate-A backlog — NOT cancelled)
- Hostinger VPS control-plane deployment (13G free constraint; hermes-only)
- Rich AgentOS dashboard integration (adapter is read-only coarse today; per-agent metrics
  + pause/resume need upstream Hermes-core work)
- Task graph UI beyond flat kanban; advanced project mgmt UI; advanced file-manager features
- Vision optimisation; 600K context testing; exhaustive context-size benchmarks
- Extensive historical charts; elaborate update management; dashboard visual polish
- Optional monitoring platforms; future cloud routing; GX10-03 support (registry extensible)
- AgentOS CC X-GX-* attribution header patch + model-routing.yaml repoint (needed at Gate C
  integration, queued as P3 platform wiring)
- LiteLLM DB virtual-key model-list trims (at gateway redeploy)
