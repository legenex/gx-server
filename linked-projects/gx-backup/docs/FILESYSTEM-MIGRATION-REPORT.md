# Filesystem migration report

Backup identifier before moves: `20260922T225812Z` (restic snapshot `dd27fe41` on gx10-01, peer copy `2a96d580` on gx10-02). Restore of `models.lock` and `litellm.sql.gz` checksum-matched before any archive move.

| Old path | Class | New path | Action | Backup | Verified |
|---|---|---|---|---|---|
| `/home/legenex/agentos-control-center-verify` | ARCHIVE | `Documents/Archive/pre-cleanup-20260922T230642Z/` | move | restic | dest exists |
| `/home/legenex/agentos-local-preserved` | ARCHIVE | same | move | restic | dest exists |
| `/home/legenex/agentos-manual` | ARCHIVE | same | move | restic | dest exists |
| `/home/legenex/agentos-pre-git-20260906-234549` | ARCHIVE | same | move | restic | dest exists |
| `/home/legenex/vscode-remote-full-reset-20260904-230140` | ARCHIVE | same | move | restic | dest exists |
| `/home/legenex/gx-review-20260917T233301Z` | ARCHIVE | same | move | restic | dest exists |
| `/home/legenex/gx-review-20260917T233301Z.tar.gz` | ARCHIVE | same | move | restic | dest exists |
| `/home/legenex/gx-cluster-review-20260917T233026Z` | ARCHIVE | same | move | restic | dest exists |
| `/home/legenex/qwen38-download.log` | ARCHIVE | same | move | restic | dest exists |
| `/home/legenex/projects` | empty | — | rmdir | n/a | gone |
| `/home/legenex/agentos` | ACTIVE PROJECT (live systemd) | kept | keep | restic | services still use it |
| `/home/legenex/Documents/Projects/AgentOS` | ACTIVE PROJECT | kept | keep | restic | git checkout |
| `/home/legenex/nccl` | UNKNOWN / toolchain | kept | keep | restic | not deleted |
| `/home/Legenex` | — | does not exist | none | — | confirmed absent |
| `/srv/models` | MODEL | kept | keep | excluded (reproducible via models.lock) | files remain |

Unknown material was archived, never deleted.
