# Obsolete AI stack removal — exact evidence (2026-09-27, PHASE 7-10)

All targets were inventoried first (INVENTORY-20260927.md), confirmed reproducible
(public weights re-downloadable; pre-V4.1 tag + gx-backup models.lock cover recovery),
and outside protected paths. Executed as narrow per-target deletions.

## gx10-01 — 697G → 359G used (≈338G recovered; final df 511G free)

Removed weights (/srv/models, all reproducible public checkpoints):
- deepseek/DeepSeek-V4-Flash-0731-CRACK (156G) — superseded old gx-max
- gguf/Ornith-1.5-35B-A3B-Uncensored (45G), gguf/Ornith-1.5-9B-uncensored-Q8_0 (9.8G)
- gguf/Qwen3.5-4B-Uncensored-HauhauCS-Aggressive + gguf/Qwen3.5-4B (6.4G)
- vllm/Qwen3.8-27B-Uncensored-NVFP4 (27G, root-owned → removed via container bind-mount)
- vllm/Qwen3.6-35B-A3B-Uncensored-NVFP4 (21G), vllm/Qwen3.8-27B-DFlash2 (4.1G)
- staging/, video/, image/ husks

Removed containers: gx-mini, gx-code, gx-llama-swap-node01 (old llama-swap gateway).
Removed images: lmsysorg/sglang:dev-v4f-2dgx-v2 (33.3G), jstarkg/vllm-gb10-flashnext:0.28-sm121-r6 (20.6G), open-webui:main (4.6G), linuxserver/ffmpeg (487M). Builder cache pruned (11.8G). Orphan net serving_default removed.
Removed units: vllm-qwen38.service, gx-playground.service (was masked).
Memory after stop: 81G → 24G used (95G available).

Home normalisation: archived (to Documents/Archive/): ~/AgentOS (stale dup), ~/worktrees,
~/si02-staging, ~/vllm-backups, ~/gx10-config-backups, ~/backups (superseded openwebui snapshot).
Moved to Documents/Projects/: ~/reachinbox-mcp. Deleted: ~/vllm (retired launcher),
~/AI, ~/models (empty husks), run-gx-max.sh, legenex-vllm.service (obsolete scripts),
Downloads duplicate installers (debs/AppImages), ~/.cache/google-chrome, npm cache.
Documents/Projects/GrowthOS (empty) removed. Verified-copy ZIPs removed from Projects root
(byte-identical protected copies exist in Documents/Backups/GX/ — checksums recorded in EVIDENCE.md).
Logs removed: /srv/logs/gx-playground, deepseek-v4-copy*.log.

## gx10-02 — 499G → 245G used (≈254G recovered; final df 625G free)

Removed weights (/srv/models): DeepSeek-V4-Flash-0731-CRACK (156G), Ornith-1.5-35B (45G),
Ornith-1.5-9B (9.8G), Qwen3.8-27B-Uncensored-NVFP4 (27G), Qwen3.8-27B-DFlash2 (3.6G),
staging husks, manifests/. /srv/ai-stack (6.8G comfyui venv, root-owned → container method).
HF hub orphaned blobs (3.6G). Chrome deb duplicates (544M), n2_run*.tsv, llama-cpp-rebuild.log.
Removed containers: gx-code, gx-llama-swap-node02; dir ~/gx-gateway.
Removed images: gx-llama-swap:latest, legenex/llama-cpp-spark:latest + pre-b011 tag.
Removed units: gx-call/gx-live/gx-music/gx-voice.service (masked → intentional decommission).
Logs removed: gx-music, gx-call, gx-voice, gx-live, comfyui.log.

## Protected (verified NOT touched)
Documents/Projects/gx-backup, Documents/Backups/GX (ZIPs + checksums), Documents/Backups/restic
(node1 183G, node2 140G), dumps/, open-webui chat DB volume, all app postgres volumes,
gx-cluster .git, /srv/projects/gx-cluster/state (secrets + guard), ~/.cache/huggingface tokens,
/srv/logs/acceptance (audit evidence), nccl/nccl-tests (needed Phase 11).

## Not removable without sudo (documented, left in place)
/swapfile-sglang 48G + /swap.img 16G (root-owned; L-8 says keep swapfile-sglang).
