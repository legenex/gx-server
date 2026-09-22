# DECISIONS

Observed machine state is authoritative. Previous gx-cluster docs are treated as hints.

## D-001 — Backup engine is restic

restic 0.18.1 (official linux/arm64 binary) plus age 1.2.1. Ubuntu restic package needed sudo which is password-gated in this session. Binaries live in `~/.local/bin` on both nodes.

## D-002 — Secrets stay off Git

Restic password and age identity: `~/.config/gx-backup/` on both nodes, mode 0600. Git holds only the age public key. Recovery uses either surviving node copy.

## D-003 — Four user-facing text modes

`gx-mini`, `gx-code`, `gx-auto`, `gx-max`. Existing gx-fast / gx-reason / DeepSeek gx-max / media aliases remain as diagnostic/internal capabilities, hidden from the normal OpenWebUI picker.

## D-004 — gx-mini model

Verified live: `junafinity/Ornith-1.5-9B-uncensored-GGUF-8bit` @ `10d5524ca2258decffa044ddf233b17fb239e838`, file `Ornith-1.5-9B-uncensored-Q8_0.gguf` sha256 `f22694e5…1573`. llama.cpp on the existing `legenex/llama-cpp-spark` image.

## D-005 — gx-code model

Verified live: `0xKitkat/Ornith-1.5-35B-A3B-Uncensored-GGUF` @ `ab0eed77c73880afda789a3914003db2273fd64a`. Start with Q5_K_M; compare Q4_K_M on real coding tests. Independent worker on each GX10. Not tensor-parallel.

## D-006 — gx-max is a dual-worker workflow

Default gx-max coordinates both gx-code workers (solver + independent reviewer + synthesis). DeepSeek SGLang TP=2 remains on disk as optional `gx-max-deepseek` and is not installed as a third always-on model.

## D-007 — Do not rebuild AgentOS, OpenWebUI, or GX-Cluster

Point them at the single LiteLLM gateway. Simplify GX-Cluster navigation. Preserve OpenWebUI chats.

## D-008 — Models live at `/srv/models`

Existing dedicated store. Not moved into Documents.

## D-009 — MVA Progress Tracker

No dedicated repository found. Integration is documented as sample config only.

## D-010 — No sudo in this session

Cannot apt-install, edit `/etc`, or reboot without a password. User systemd, Docker, and `~/.local` are available.
