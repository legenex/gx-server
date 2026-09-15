<!--
Reference copy for uploading into the ChatGPT Project named "GX-Cluster".
Network/hardware facts here are also captured in OPERATIONS.md ("Node
addresses") and CURRENT_STATE.md ("Hardware"), which are re-verified against
the live machines and take precedence if this ever drifts.
-->

# GX-Cluster Project Context

## Goal

Build a resilient, agent-operable, two-node local AI platform using two ASUS Ascent GX10 / NVIDIA DGX Spark systems.

The cluster is intended for:

- local reasoning and coding models
- agent workflows and Hermes/Buzz integration
- multimodal inference
- image generation and editing
- video generation
- remote project building while travelling
- OpenAI-compatible APIs for tools and agents

## Hardware

### GX10-01

- Hostname: `gx10-01`
- Linux user: `legenex`
- DGX Spark Version: 7.5.0
- GPU: NVIDIA GB10
- Unified memory: 128 GB class
- Kernel: `6.17.0-1032-nvidia`
- NVIDIA driver: `580.173.02`
- CUDA host: 13.0
- LAN: `10.60.21.37`
- Tailscale: `100.105.214.61`
- ConnectX rail 1: `192.168.100.10`
- ConnectX rail 2: `192.168.101.10`

### GX10-02

- Hostname: `gx10-02`
- Linux user: `legenex-02`
- DGX Spark Version: 7.5.0
- GPU: NVIDIA GB10
- Unified memory: 128 GB class
- Kernel: `6.17.0-1032-nvidia`
- NVIDIA driver: `580.173.02`
- LAN: `10.60.21.41`
- Tailscale: `100.73.238.4`
- ConnectX rail 1: `192.168.100.11`
- ConnectX rail 2: `192.168.101.11`

## Mac management device

- Tailscale IP: `100.104.35.71`

## Cluster network

Two ConnectX-7/RoCE rails are configured and tested between the GX10s.

NCCL traffic must use ConnectX, not Tailscale.

DGX Spark does not support GPUDirect RDMA in this topology. NET/IB with staged shared/pinned memory is expected.

## Canonical project path

GX10-01:

`/home/legenex/Documents/Projects/Server/gx-cluster`

Parent:

`/home/legenex/Documents/Projects/Server`

Symlink:

`/srv/ai-stack -> /home/legenex/Documents/Projects/Server`

Persistent storage:

- `/srv/models`
- `/srv/cache`
- `/srv/logs`

## Canonical repo

Branch:

`legenex-dual-gx10`

Community base repo adapted by this project:

`mARTin-B78/dgx-spark_lite-llm_llama-swap_vllm_llama-cpp_ollama`

This upstream is primarily single-node and must not be applied blindly.
