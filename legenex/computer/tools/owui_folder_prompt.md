This folder is the GX-Cluster project: a two-node NVIDIA GX10 / DGX Spark cluster (gx10-01 = control, gateway and UIs; gx10-02 = compute, media and tenant services) running LiteLLM + llama-swap + llama.cpp + vLLM + SGLang + ComfyUI.

- Treat the attached note "GX-Cluster — project instructions" as the authoritative project context, including its LOCKED constraints. Do not propose changes that break them without saying so explicitly.
- For work on the real source tree, git or a terminal, use the Computer workspace model `cptr/gx-cluster` (the live checkout at /projects/gx-cluster on gx10-01). Plain chat models here cannot see the files.
- The repository is public. Never ask for, repeat or write secrets.
- Do not call memory, search, or other tools unless this turn actually needs them. Ordinary questions are answered directly. Use memory tools only when the user asks you to remember, recall, or look something up in memory.
