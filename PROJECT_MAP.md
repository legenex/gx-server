# Project map — gx-cluster (two-node GX10)

**Current phase:** production operation of all seven tiers, with automated
source control. Version: see `VERSION`.
**Canonical remote:** https://github.com/legenex/gx-server (`main`). This
repository is **public**.

## Where things are

| Path | What it is |
|---|---|
| `CLAUDE.md` | Operating rules and the LOCKED constraints table (L-1..L-10) |
| `CURRENT_STATE.md` | What is actually running now. Read first. |
| `ARCHITECTURE.md` | Locked decisions and why |
| `coordination/DECISIONS.md` | Decision log (D-001..) |
| `coordination/BLOCKERS.md` | Open and resolved blockers (B-001..) |
| `TEST_RESULTS.md` | Only results that were actually observed |
| `OPERATIONS.md`, `RECOVERY.md`, `Manual.md` | Operator documentation |
| `legenex/gateway/` | LiteLLM + llama-swap configs and compose files (node 1 gateway, node 2 worker) |
| `legenex/orchestrator/` | gx-auto routing and gx-max lifecycle service (stdlib Python), with tests |
| `legenex/lifecycle/` | gx-max start/stop/unwind/status, the safety rules, both watchdogs, the resource guard |
| `legenex/media/` | gx-image / gx-video router and ComfyUI compose (node 2) |
| `legenex/host/` | Host watchdog and the kernel-lock tooling |
| `legenex/tests/` | Acceptance, unwind regression, gx-max validation and inference suites |
| `ops/git-sync/` | Writer, mirror and audit tooling for source control (D-026) |
| `.githooks/` | Versioned Git hooks (writer only) |

## Implemented

* **Seven gateway aliases:** gx-mini, gx-fast, gx-reason, gx-max, gx-auto,
  gx-image, gx-video.
* **gx-max** on SGLang, TP=2 across both nodes over RoCE, with:
  * a cluster-takeover admission policy;
  * phase-aware safety rules on both nodes;
  * a deadman on node 2 and a watcher on node 1;
  * a verified unwind.
* **Resource guard:** flock plus residency ledger, with a 30 GiB reserve for
  single-node tiers.
* **Git sync:**
  * the only writer auto-commits and pushes;
  * the mirror reconciles to `origin/main`;
  * a daily audit runs on both nodes.

## Runtime layout (outside Git)

| Path | Contents |
|---|---|
| `/srv/models` | weights, a separate copy per node |
| `/srv/logs` | logs; `gx-git-sync/` holds sync logs and drift evidence |
| `/srv/projects/gx-cluster/state` | guard locks and ledgers, git-sync role and lock, watcher pid |
| `/srv/projects/gx-cluster/secrets` | mode 0700; machine-local secrets |
| `/srv/projects/gx-cluster/backups` | pre-migration Git bundle |
| `legenex/gateway/.env`, `legenex/media/.env` | ignored; live keys |

## Environment

* No sudo.
* Everything runs through Docker (CDI GPU:
  `--device nvidia.com/gpu=all`) and `systemctl --user`.
* Kernel pinned to `6.17.0-1032-nvidia`; verify with
  `legenex/host/kernel-lock/verify-kernel-lock.sh`.
* SSH to node 2: `legenex-02@gx10-02` (Tailscale, management only).

## Pending decisions and limitations

* **B-023:** node 1 reaches the swap ceiling during the gx-max load. Should
  the gx-max drain also stop Open WebUI and AgentOS?
* **B-015:** sshd, tailscaled and friends cannot be OOM-protected without
  root.
* **B-016:** there is no remote power-cycle path.
* **Server-side status:** GitHub branch protection and secret scanning are
  not configured from here.

## Test commands

```bash
(cd legenex/orchestrator && python3 -m unittest discover -s tests)
(cd legenex/lifecycle && python3 -m unittest discover -s tests)
(cd legenex/media/router && python3 -m unittest discover -s tests)
legenex/tests/unwind-tests.sh E1 E2 E3 E4 E6         # non-destructive
legenex/tests/acceptance.sh                          # live tiers
legenex/tests/gx-max-inference.sh                    # against a running gx-max
ops/git-sync/integrity-audit.sh
```

## Next logical step

Decide B-023. Then consider GitHub branch protection that allows pushes only
from gx10-01's credential.
