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
- NCCL_IB_GID_INDEX=3 is REQUIRED on this cluster (RoCEv2 IPv4 GID). Without it QP setup
  silently hangs. Set for any distributed GPU job (add to Mia launch env in Phase 15).
- OpenMPI -H does not accept user@host; use ~/.ssh/config Host alias with User instead.
- pkill/pgrep -f patterns self-match the calling shell: use exact-PID kills or script files.
- /srv is root-owned; writable subdirs are /srv/models (both nodes), /srv/logs, /srv/projects/gx-cluster.
  For cross-node common paths use /srv/models/<dir>.
- Host NCCL 2.30.7+cuda13.0 built at ~/nccl/build on BOTH nodes; nccl-tests built on both.
