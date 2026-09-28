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

- 2026-09-27 (fast-track P1, corrected): overlay gate still unaccepted (file resolve 403, gated=auto). Evidence-based path stays dealignai CRACK (ungated, drop-in, validated serving notes; HarmBench 99.4%, non-ethics MMLU -0.58pp, DSpark ~45%). Sequence: stock battery -> delete stock pack (disk) -> download dealignai 210G -> load -> uncensor-verify battery -> production select. If the user accepts the drowzeys gate later, the 650MB overlay is the cheaper alternative for a re-test.

## D-M5 Profiles from Gate A measurement (2026-09-28)
FAST: max_num_seqs=1, DSpark k=3. Measured TTFT 233ms, decode 14.6 tok/s.
BALANCED: max_num_seqs=2, DSpark k=3. Measured ~11.8 tok/s/stream, aggregate 22.7.
SWARM: keep max_num_seqs=2 (no aggregate gain at 4); orchestrator queues excess up to 4.
  4 concurrent completed with 0 errors (agg 21.4) — stable, not faster.
Do not load a second copy of the model.

## D-M6 Disk: replace stock with dealignai after Gate A
Head has 79G free. Stock 197G and HF cache are the SAME inodes (hardlink nlink=2);
deleting cache alone frees 0 bytes. After Gate A evidence+manifest+commit, stop
ranks, delete head /srv/models/dsv41/model AND the hardlinked cache copy, keep
engram, download dealignai to /srv/models/dsv41/uncensored (~210G). Recovery:
worker still has an independent local stock copy at /srv/models/dsv41/model;
re-download Mia-AiLab/DeepSeek-V4.1-Flash-EXL3-2.9bpw if needed.
