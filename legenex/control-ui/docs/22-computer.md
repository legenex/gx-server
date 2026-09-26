# Open WebUI Computer

Computer runs on gx10-01 as container `gx-computer`. It is already set up; no
UI configuration is needed.

| | |
|---|---|
| URL | `http://100.105.214.61:8000` (Tailscale) or `http://gx10-01:8000` |
| Version | 0.9.21 |
| Account | the Open WebUI admin account: same login name, display name and avatar, with its own Computer password |
| Workspace | **GX-Cluster** = `/projects/gx-cluster` (this repo, live) |
| Models | `gx-auto` (default), `gx-mini`, `gx-code`, `gx-max` through LiteLLM key `gx-computer` |
| In Open WebUI | model **GX-Cluster - /projects/gx-cluster** (`cptr/gx-cluster`), admins only |
| Project instructions | `.cptr/system.md` + `CLAUDE.md`, injected automatically |

Computer chats, logs and attachments stay in the workspace's `.cptr/` folder.
It is gitignored, so they never reach the public repository.

Re-apply or check the integration from a shell on gx10-01:

```
cd legenex/computer/tools
python3 provision.py        # idempotent
python3 verify.py           # live end-to-end checks
```

Full operator notes: `legenex/computer/README.md`.
