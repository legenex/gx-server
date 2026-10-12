# Recovery secret

The encrypted restic repositories on both machines are unlocked with a
password stored locally at:

```
~/.config/gx-backup/restic-password
```

Permissions must remain `0600`. The matching `age.key` lives in the same
directory, also `0600`. Neither file is committed. `scripts/secret-scan.py`
blocks accidental commits of these names.

Both GX10-01 and GX10-02 must use the **same** restic password. They are
compared by SHA-256 of the password files, never by printing the secret.

## What this secret unlocks

- GX10-01 local restic repo: `~/Documents/Backups/restic/local`
- GX10-02 local restic repo: `~/Documents/Backups/restic/local`
- Cross-node recovery copies:
  - GX10-01 holds `~/Documents/Backups/restic/peer-gx10-02`
  - GX10-02 holds `~/Documents/Backups/restic/peer-gx10-01`

GitHub `legenex/gx-backup` is the rebuild **recipe**. It cannot decrypt
restic data.

## Total dual-machine loss

If both GX10 nodes are gone, the restic copies on those nodes are gone
with them. Restoring mutable data then requires:

1. An independent copy of `restic-password` (and ideally `age.key`)
2. An independent copy of the restic repository itself (offsite)

There is no password manager or secret store already configured on these
machines (`pass`, Bitwarden CLI, 1Password CLI are absent). No new paid
service was added.

**This is the one recovery item you should store externally yourself.**
Keep a copy of `restic-password` (and `age.key`) in an existing password
manager or offline sealed note that does not live only on GX10-01/GX10-02.

Do not paste the password into chat, issues, Git, or `docs/`.

## Offsite mutable data

Cross-node restic is **not** offsite. See `docs/BACKUP-COVERAGE.md`.

```
OFFSITE MUTABLE DATA: NOT CONFIGURED
```
