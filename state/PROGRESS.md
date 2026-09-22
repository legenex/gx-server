# PROGRESS

Updated: 2026-09-23T00:50:00Z (SAST +02:00)

## Complete

- Dual-node audit, restic+age, cross-node encrypted snapshots, restore tests
- Private GitHub `legenex/gx-backup`
- `gx` CLI: status, backup, doctor, models, mini/code/auto/max, logs, benchmark
- Ornith gx-mini Q8_0 resident; Ornith gx-code Q5_K_M resident on gx10-01
- Gateway aliases gx-auto / gx-mini / gx-code / gx-max
- gx-max dual-worker (solver+reviewer) live: 17*19=323
- OpenWebUI picker limited to four modes; chats preserved
- GX-Cluster nav + Backup page
- Docs + 2-page recovery PDF
- Daily `gx-backup.timer`

## Remains / known gaps

- No sudo: reboot persistence not proven from this session
- No independent offsite mutable backup
- MVA Progress Tracker not found
- Hermes VPS not reconciled remotely
- gx-code Ornith on gx10-02 not yet the live worker (gx-code-02 still gx-reason)
- Six-task coding acceptance suite not fully executed against Ornith
