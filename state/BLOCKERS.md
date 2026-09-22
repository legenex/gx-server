# BLOCKERS

## B-001 — Passwordless sudo unavailable

`sudo -n` fails. Cannot install distro packages, write `/etc`, manage system systemd units, or reboot from this session. Workaround: restic/age binaries in `~/.local/bin`; user systemd; Docker (user is in `docker` group).

## B-002 — Independent offsite mutable-data backup

No authorized paid offsite destination found. Plan: private GitHub recipe + encrypted cross-node restic. Fully independent offsite mutable backup remains absent unless an existing destination is discovered later.

## B-003 — MVA Progress Tracker project not found

Searched home, Documents/Projects, AgentOS. No dedicated tracker repo. Will document sample integration rather than rewrite a missing app.

## B-004 — `/home/Legenex` does not exist

Only `/home/legenex` and `/home/odin-agent`. Case-sensitive path mentioned in the brief is absent on gx10-01.
