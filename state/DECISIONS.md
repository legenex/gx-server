# Mission Decisions — GX10 DeepSeek V4.1 Flash Clean Rebuild

Supersession note: mission brief (user, 2026-09-27) explicitly supersedes old
locked decisions L-6/L-10 (SGLang TP=2 NVFP4 gx-max, eleven aliases). The old
CLAUDE.md rows remain for history; this file wins for the rebuild.

## D-M1 Architecture supersession
Old gx-max (nvidia/DeepSeek-V4-Flash-0731-NVFP4, SGLang TP=2) is REPLACED by
DeepSeek V4.1 Flash EXL3 ~2.9bpw via MiaAI-Lab 2x DGX Spark runtime
(https://github.com/MiaAI-Lab/DeepSeek-v4.1-Flash-EXL3-2x-DGX-Sparks).
Public aliases after rebuild: gx-max, gx-auto ONLY. gx-mini/gx-fast/gx-reason/
gx-code retired. User explicitly approved (mission brief §5, §7).

## D-M2 Unchanged constraints
Kernel 6.17.0-1032-nvidia pinned. Driver 580.173.02 pinned. No firmware
changes. Tailscale = management only; ConnectX/RoCE = distributed inference.
Two separate 128GB nodes. gx10-01 head, gx10-02 compute. No sudo — Docker +
systemctl --user. CDI GPU passthrough. Large model never auto-loads at boot.

## D-M3 Recovery protection (done)
Pre-V4.1 tags pushed to both repos: `pre-deepseek-v41-rebuild-20260927`.
Manual ZIPs protected under /home/legenex/Documents/Backups/GX/ with verified
sha256 (see EVIDENCE.md).

## D-M4 Uncensoring strategy
Evaluate in order: (1) stock Mia baseline + benchmark, (2) drowzeys abliterated
overlay + keys helper (storage-efficient), (3) dealignai full UNCENSORED EXL3
2.9bpw checkpoint only if overlay insufficient and storage permits. Production
gx-max must be genuinely uncensored; no silent stock fallback. Full provenance
recorded.
