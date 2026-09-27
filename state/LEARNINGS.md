# Mission Learnings — GX10 DeepSeek V4.1 Flash Clean Rebuild

- No sudo on either node. All privileged-looking work must go through Docker
  or systemctl --user.
- GPU is CDI-only (`--device nvidia.com/gpu=all`); --gpus/--runtime nvidia fail.
- Fabric addresses are not SSH endpoints; node2 SSH goes over Tailscale.
- gx-cluster checkout auto-commits after ~45s quiet and pushes to a PUBLIC
  repo. Secrets must stay in /srv/projects/gx-cluster/secrets or .env.
- Old stack discipline: model lifecycle through Resource Controller /
  ActionRunner; never docker stop directly; never call ComfyUI /free directly.
- Old gx-max rollback checkpoint (nvidia/...-NVFP4) was already deleted
  2026-09-17 (B-026); dealignai/DeepSeek-V4-Flash-0731-CRACK-NVFP4 was the
  serving checkpoint at mission start.
