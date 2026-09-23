# GX cluster closeout — final report

**Date:** 2026-09-23  
**Project status:** CLOSED (core cluster accepted)

This report uses three labels only:

| Label | Meaning |
|---|---|
| **VERIFIED PASS** | Exercised live and met the requirement |
| **DEFERRED BY DESIGN** | Optional / out of scope; not a failure |
| **BLOCKED BY EXTERNAL AUTHORITY** | Needs a human (sudo/password) this session cannot supply |

## Verdict

The dual GX10 text cluster is operational: four modes on one gateway, independent Ornith workers on both nodes, encrypted cross-node restic, OpenWebUI, AgentOS Control Center, and GX-Cluster UI. Remaining items are reboot (needs sudo) and true offsite mutable backup (not configured; not a reason to keep the project open).

## VERIFIED PASS

| Item | Evidence |
|---|---|
| GX10-01 health | `gx doctor` 13/13; hostname gx10-01; 121 GiB; NVMe 78% (193 G free); Tailscale 100.105.214.61; RoCE both rails |
| GX10-02 health | SSH `gx10-02`; 121 GiB; NVMe 89% (102 G free after reclaiming unused Docker images); Tailscale 100.73.238.4 |
| gx-mini | Ornith-1.5-9B Q8_0 llama.cpp, resident, `Pong` 0.35–0.44 s |
| gx-code-01 | Ornith-1.5-35B-A3B Q5_K_M llama.cpp on gx10-01; `NODEONE` 0.22 s |
| gx-code-02 | Same Q5 on gx10-02; `NODETWO` 0.27 s |
| gx-auto | Classifier unit tests OK; live routing: greeting/summary/reason → gx-mini; light script / substantial coding / debugging → gx-code |
| Busy coding | Occupied both workers; third substantial coding request stayed on **gx-code** (11.9 s, queued). Did not fall to gx-mini |
| gx-max | Dual-worker on real `longest_unique` bug: solver=gx-code-01, reviewer=gx-code-02, verdict fail then repair; traces for `abba`/`pwwkew`/`bbbbb` |
| OpenWebUI | Picker gx-auto, gx-mini, gx-code, gx-max; default gx-auto; 24 chats / 116 messages preserved; restart always; live prompt each mode 200 |
| AgentOS | Control Center `:4173`, `AGENTOS_CC_LLM_MODEL=gx-code`; `/api/ask-live` used gx-code; two concurrent asks both gx-code |
| GX-Cluster UI | Playwright pages Dashboard/Models/Jobs/Logs/Machines/Backup/Settings against live backend; backup latest `20260923T090136Z`; Verify runs real `restic check` |
| Backup | See recovery point below. Restic self-inclusion of the repo is now excluded |
| Coding suite | **5/6 PASS, 1 PARTIAL, 0 FAIL** (Q5). No Q4 comparison required |
| Concurrency | Simultaneous gx-code-01 (1.85 s, 51 t/s) and gx-code-02 (2.10 s, 52 t/s); both stable; load-balance after OK |
| Recovery secret files | Both nodes `0600`; SHA-256 of password files match; not in git |

### Latest verified recovery point

Stamp **`20260923T091634Z`** (triggered by GX-Cluster UI `POST /api/backup/now` → `backup/restic.sh`).  
`restic check --read-data-subset=2%`: no errors. `models.lock` restore sha256 match.

| Copy | Snapshot |
|---|---|
| GX10-01 local | `24ba5393` (22.167 GiB) |
| GX10-02 local | `fa6b8856` (9.235 GiB) |
| GX10-01 recovery copy on GX10-02 | `f5588ad0` |
| GX10-02 recovery copy on GX10-01 | `bbee85a2` |

Earlier known-good points (`4ebe7225` / `20260923T090136Z`, `dd27fe41` / `20260922T225812Z`, and others) were **not** deleted.

## DEFERRED BY DESIGN

| Item | Notes |
|---|---|
| True independent offsite mutable-data backup | **OFFSITE MUTABLE DATA: NOT CONFIGURED**. GitHub is recipe-only. Cross-node restic is not offsite |
| MVA Progress Tracker | Not present locally. Cluster endpoint ready. Intended routing documented |
| Hermes VPS reconciliation | Hermes lives elsewhere. Local yaml/gateway documented. Remote apply **DEFERRED** |
| DeepSeek TP=2 gx-max | Optional; dual-worker is the accepted gx-max |
| Media/image/video stack | Media router on gx10-02 is unhealthy (HTTP 503). Out of this closeout’s text-cluster scope; UI overall=warn because of it |

## BLOCKED BY EXTERNAL AUTHORITY

| Item | Exact gap |
|---|---|
| Reboot validation | `sudo -n true` → `sudo: a password is required`. Cannot `reboot` either node from this session |

Commands that could not be run:

```
sudo -n true
sudo reboot          # on gx10-02 first, then gx10-01, one at a time
```

When sudo is available, reboot GX10-02 first, wait for SSH/Tailscale/RoCE/docker/gx-code-02, then reboot GX10-01, then `gx doctor` and `gx status`.

## Coding reliability (not speed)

Ornith Q5_K_M llama.cpp, both workers. First-attempt pytest after a missing-pytest harness bug was fixed:

| # | Task | Node | t/s | Files | Tests | Rescue | Verdict |
|---|---|---|---|---|---|---|---|
| 1 | Bug repair (`median`) | GX10-01 | 45 | stats.py | 4 passed | no | PASS |
| 2 | Frontend conventions | GX10-02 | 56 | index.html, app.js | 5 passed | no | PASS |
| 3 | Backend `create_item` | GX10-01 | 45 | api.py | 3 passed | no | PASS |
| 4 | Multi-file tax refactor | GX10-02 | 55 | tax.py, user.py, order.py | 3 passed | no | PASS |
| 5 | Integration `userId`→`user_id` | GX10-01 | 43 | producer.py | 2 passed | no | PASS |
| 6 | Autonomous calc library | GX10-02 | 52 | ops.py | 4 passed after one repair (empty/zero) | no | PARTIAL |

**Overall: 5/6 PASS, 1 PARTIAL.** Suitable for local coding with occasional edge-case repair. Not declared suitable because it is fast (~45–56 tok/s).

## Disk note (GX10-02)

Node 2 was at 100% (15 MB free). Unused (not running) Docker images were pruned (~102 GB), `legenex/llama-cpp-spark:latest` retagged from the running gx-code image. This was required for cross-node restic copy. Media engine images are gone from that node and would need rebuild if media is started again.

## Commands

```
gx status
gx backup
gx doctor
gx auto
gx mini
gx code
gx max
gx logs
```
