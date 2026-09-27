# Full inventory snapshot — 2026-09-27 (pre-cleanup)

Three parallel read-only agents inventoried gx10-01, gx10-02 (SSH), and /home/legenex.
Raw agent reports preserved in this file's history (git). Key facts below drive PHASE 7-10.

## Deletion plan — obsolete reproducible AI stack (mission §7 authorized)

### gx10-01 /srv/models (268G → ~0)
| Path | Size | Status |
|---|---:|---|
| deepseek/DeepSeek-V4-Flash-0731-CRACK | 156G | old gx-max SGLang checkpoint, superseded by D-M1. Nothing references it. DELETE |
| gguf/Ornith-1.5-35B-A3B-Uncensored (Q4_K_M 21.7G + Q5_K_M 25.3G + mmproj 0.9G) | 45G | old gx-code. Retire container, then DELETE |
| gguf/Ornith-1.5-9B-uncensored-Q8_0 | 9.8G | old gx-mini. Retire container, then DELETE |
| gguf/Qwen3.5-4B-Uncensored-HauhauCS-Aggressive + Qwen3.5-4B | 6.4G | no live refs. DELETE |
| vllm/Qwen3.8-27B-Uncensored-NVFP4 | 27G | retired vllm-qwen38 (disabled unit). DELETE |
| vllm/Qwen3.6-35B-A3B-Uncensored-NVFP4 | 21G | gx-fast-vllm-retired swap def. DELETE |
| vllm/Qwen3.8-27B-DFlash2 | 4.1G | no live refs. DELETE |
| staging/ husks | ~0 | DELETE |

### gx10-01 Docker
- REMOVE images: lmsysorg/sglang:dev-v4f-2dgx-v2 33.3G (old gx-max runtime), jstarkg/vllm-gb10-flashnext:0.28-sm121-r6 20.6G (old gx-fast), open-webui:main 4.6G (unused), linuxserver/ffmpeg 487M (old media), nvidia/cuda base 271M (old builds).
- PRUNE build cache 11.8G.
- REMOVE containers: gx-mini, gx-code (old models), gx-llama-swap-node01 (no models left after retirement; Mia has own lifecycle; §21).
- KEEP: gx-litellm, gx-litellm-db, open-webui (user chat data), gx-computer, gigpilot*, pageflo*, financialos* (unrelated user projects), playwright image (needed for new dashboard e2e).
- KEEP (small, user-data risk, review later): orphan vol open-webui_open-webui 731MB, anon 435c 148MB, d918 48MB — old Open WebUI pre-migration data; live volume + restic backups exist; do NOT delete this pass.
- KEEP stopped stacks: reachinbox-mcp, datascraper, financialos-test (unrelated user projects, §35 "do not blindly prune unrelated project Docker resources").

### gx10-01 systemd user units
- REMOVE: vllm-qwen38.service (disabled/dead, refs ~/vllm), gx-playground.service mask+file, gigpilot-ts-proxy@.1... failed leftovers (not AI but stale — remove).
- KEEP: gx-orchestrator, gx-control-ui, gx-gateway-ts-proxy, gx-backup, gx-hostwatch, gx-git-*, agentos-* (repoint LLM later), gigpilot, wiki units.

### gx10-01 home
- DELETE: ~/vllm (launcher for retired service), ~/AI + ~/models (empty husks), ~/Downloads duplicate installers (~1.5G of reproducible debs/appimages), ~/.cache/google-chrome 983M, npm cache.
- ARCHIVE: ~/AgentOS (stale dup of Documents/Projects/AgentOS), ~/worktrees, ~/si02-staging, ~/vllm-backups, ~/gx10-config-backups, ~/backups (superseded openwebui snapshot).
- MOVE: ~/reachinbox-mcp → Documents/Projects/.
- DELETE later (after ConnectX/NCCL validation Phase 11): ~/nccl 313M + ~/nccl-tests 116M (recloneable).
- KEEP in place: services/ (wiki dashboards, systemd-wired), bin/cloudflared (verify later), gx-kernel-lock, Applications, Obsidian, Downloads unique items.
- KEEP: Documents/Backups (183G restic + 13G dumps — PROTECTED), Documents/Archive, all Projects.
- Projects root: remove the two ZIPs after verified copies (they exist byte-identical in Documents/Backups/GX/).
- Swap files /swapfile-sglang 48G + /swap.img 16G root-owned: CANNOT remove without sudo; L-8 says keep swapfile-sglang. Left in place (documented).

### gx10-02 /srv + Docker + units
- DELETE: /srv/models ENTIRE old set (DeepSeek-V4-Flash-0731-CRACK 156G, Ornith 45G, Ornith-9B 9.8G, Qwen3.8 NVFP4 27G, Qwen3.8-DFlash2 3.6G, staging husks, manifests/) — all obsolete after gx-code retirement.
- DELETE: /srv/ai-stack (comfyui venv 6.8G, pip-reproducible), ~/.cache/huggingface/hub blobs 3.6G (orphaned), duplicate chrome debs 544M, n2_run*.tsv old benchmarks, llama-cpp-rebuild.log.
- REMOVE containers: gx-code, gx-llama-swap-node02; then ~/gx-gateway dir (old gateway compose+config).
- REMOVE masked units: gx-call/live/music/voice.service (mask symlinks + files — decommission intentionally).
- KEEP: restic 140G (PROTECTED), git mirror + reconcile units, hostwatch, guard state, ~/.cache/huggingface/{token,stored_tokens,xet}.
- /srv/projects/gx-cluster/state/* engine.env (mode 600): KEEP (secrets), will be superseded by new lifecycle.

### /srv/logs (both nodes)
- DELETE: old playground/media logs (gx-music 11M, gx-call 3.7M, gx-voice 2.8M, gx-live 2.5M node2; /srv/logs/gx-playground 2.7M node1), old acceptance dirs 236M (old-stack evidence; the pre-V4.1 git tag + restic cover recovery), deepseek-v4-copy logs, comfyui.log.
- KEEP: gx-orchestrator.log, gx-hostwatch.log, gx-text, gx-control-ui, gx-git-sync, gx-max-evidence-node2 (52K historical record).

## User data protected (NOT touched)
restic repos (node1 183G local + peer 46G; node2 140G), all postgres/open-webui/gigpilot/financialos/pageflo volumes, Documents/Backups/dumps, /srv/projects/gx-cluster/state, ~/.config/gx-backup, Open WebUI chat DB volume, Archive/.

## Disk projection after cleanup
- gx10-01: 697G used − 268G models − ~70G docker − ~2.5G misc ≈ ~356G used → ~560G free (need ~390G for V4.1 weights + headroom for overlay copy)
- gx10-02: 499G used − 241G models − 6.8G venv − 4G misc ≈ ~247G used → ~669G free (need ~390G local replica if WEIGHT_SYNC=rsync)
