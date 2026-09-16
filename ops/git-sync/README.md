# gx-cluster Git sync

This page is for whoever operates the two-node cluster. It covers how source
changes flow from gx10-01 to GitHub and then to gx10-02, and what to do when
that flow stops. The design decision is recorded in
`coordination/DECISIONS.md` D-026.

```
edit on gx10-01 ──45 s quiet──► safety + secret scan ──► commit ──► push origin/main
                                                                  │
                                           ssh: start gx-git-reconcile.service
                                                                  ▼
                                    gx10-02: fetch ──► save drift evidence ──► reset to origin/main
fallbacks: gx10-01 autosync timer every 1 min · gx10-02 reconcile timer every 1 min
daily:     integrity-audit.sh on both nodes
```

| | gx10-01 | gx10-02 |
|---|---|---|
| Checkout | `/home/legenex/Documents/Projects/Server/gx-cluster` | `/home/legenex-02/Documents/Projects/Server/gx-cluster` |
| Role file | `/srv/projects/gx-cluster/state/git-sync/role` = `writer` | same path = `mirror` |
| Remote | `origin` = `https://github.com/legenex/gx-server.git` (push through the `gh` credential helper) | same fetch URL; push URL is `DISABLED-gx10-02-is-pull-only` |
| Units (`systemctl --user`) | `gx-git-watch.service`, `gx-git-autosync.timer`, `gx-git-daily-audit.timer` | `gx-git-reconcile.timer`, `gx-git-daily-audit.timer` |
| Hooks | `core.hooksPath=.githooks` (pre-commit, post-commit, pre-push) | none |
| Logs | `/srv/logs/gx-git-sync/` | `/srv/logs/gx-git-sync/` (drift evidence in `drift/`, mode 0700) |

## Install or repair

```bash
# gx10-01
ops/git-sync/install-sync.sh writer
# gx10-02
ops/git-sync/install-sync.sh mirror
# either node
ops/git-sync/install-sync.sh status
```

The installer is idempotent and uses user units only; no root is needed.

## What gets committed automatically

gx10-01 commits everything that Git does not ignore, once no changed file has
been touched for 45 seconds. Each commit is named
`autosync(gx10-01): YYYY-MM-DD HH:MM:SS`.

A pass **does not commit** in any of these cases:

* The node is not the writer.
* The checkout is not on `main`.
* A merge, rebase, cherry-pick, revert or bisect is in progress, or `index.lock` exists.

A pass **commits nothing and unstages the change** in any of these cases:

* A staged change contains conflict markers.
* The secret scan reports a finding.
* The secret scanner is broken.

These paths are **never committed**; they are unstaged and logged by name in
`rejected-paths.log`:

* `.env` files
* keys
* model weights
* archives
* swap files
* ledgers and pid files
* anything under `.state/`, `secrets/` or `node_modules/`
* files over 5 MiB

**A manual commit** runs through the same pre-commit checks. The post-commit
hook then pushes it within seconds.

**GitHub down:** commits stay local and each failure is appended to
`push-failures.log`. The next pass (at most one minute later) retries.

## Never in Git

* Model weights. They stay in `/srv/models`, a separate copy per node.
* Caches.
* Logs. They go to `/srv/logs`.
* Generated media.
* Swap files.
* Runtime state. It lives in `/srv/projects/gx-cluster/state`.
* Secrets. They live in `/srv/projects/gx-cluster/secrets` (mode 0700) or in
  ignored files such as `legenex/gateway/.env`.

## Drift on gx10-02

gx10-02 is a mirror, so any local edit there is drift. When the reconciler
finds drift, it does three things:

1. Writes `drift/<timestamp>/`, containing:
   * `status.txt`
   * `files.txt`: the affected paths, with sha256 and size
   * `tracked.patch`, with secret-like values masked
   * `local-commits/`
   * `untracked/`
2. Logs the affected file names.
3. Resets the checkout to `origin/main`.

The reset uses `git clean -fd`, so ignored files (for example
`legenex/media/.env`) are kept.

To make a change that should stay on gx10-02, make it on gx10-01.

## Checks

```bash
ops/git-sync/integrity-audit.sh              # full audit now; exit 0 = all PASS
cat /srv/logs/gx-git-sync/audit-latest.log
journalctl --user -u gx-git-watch -n 50      # gx10-01
journalctl --user -u gx-git-reconcile -n 50  # gx10-02
git -C <checkout> rev-parse HEAD; git ls-remote https://github.com/legenex/gx-server.git main
```

## Stopping it

```bash
# gx10-01
systemctl --user disable --now gx-git-watch.service gx-git-autosync.timer
# gx10-02
systemctl --user disable --now gx-git-reconcile.timer
```

Nothing else depends on these units. Stopping them only stops the
synchronisation.

## Rollback

The pre-migration history (all refs) is at
`/srv/projects/gx-cluster/backups/gx-server/pre-github-migration.bundle`. The
same history is on GitHub under the tag `pre-github-migration-20260916`.

gx10-02's previous non-Git copy is kept next to the checkout as
`gx-cluster.pre-git-<timestamp>`.

_Sync path last exercised end to end: 2026-09-16 13:44 SAST (TEST 1, D-026)._
