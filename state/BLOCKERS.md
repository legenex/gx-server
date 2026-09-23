# BLOCKERS

## B-001 — Passwordless sudo unavailable (reboot)

`sudo -n true` → `sudo: a password is required`. Reboot validation is the only remaining human-authority blocker for cluster closeout.

Could not run:

```
sudo -n true
sudo reboot    # gx10-02 first, then gx10-01
```

## B-002 — Independent offsite mutable-data backup

No authorized paid offsite destination found. Plan: private GitHub recipe + encrypted cross-node restic. Fully independent offsite mutable backup remains absent unless an existing destination is discovered later.

## B-003 — MVA Progress Tracker project not found

Searched home, Documents/Projects, AgentOS. No dedicated tracker repo. Will document sample integration rather than rewrite a missing app.

## B-004 — `/home/Legenex` does not exist

Only `/home/legenex` and `/home/odin-agent`. Case-sensitive path mentioned in the brief is absent on gx10-01.
