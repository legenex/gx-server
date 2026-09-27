# Mission Evidence — GX10 DeepSeek V4.1 Flash Clean Rebuild

## 2026-09-27 — Recovery asset protection (PHASES 3-5)

gx-cluster Git: branch main == origin/main at autosync 6113144 (2026-09-27
00:44). Tag `pre-deepseek-v41-rebuild-20260927` created + pushed →
github.com/legenex/gx-server (verified push output).

gx-backup Git: was 3 modified package-manifest files (auto-refresh);
committed as `autosync: refresh package manifests...` then tag
`pre-deepseek-v41-rebuild-20260927` created + pushed →
github.com/legenex/gx-backup (5dd38ee..a65852f + new tag).

Manual archives (originals kept in place; copies verified byte-identical):
| File | Size (B) | SHA-256 |
|---|---|---|
| gx-backup.zip | 23,196,389 | 8a79456abde16a81182e8dd715217bbe90698199760252b681675b8be4757221 |
| Server Backup.zip (gx-cluster incl .git) | 150,658,586 | cf75c6607148f2f1dd5018dead19352b88a4a05128d72efd9d6383e56f30330c |

Protected copies: /home/legenex/Documents/Backups/GX/{gx-backup.zip,Server Backup.zip}

Node ground truth (2026-09-27): gx10-01 / = 916G, 697G used, 173G avail,
/srv/models 268G. gx10-02 / = 916G, 499G used, 371G avail, /srv/models 241G,
/srv/ai-stack 6.8G. Kernel 6.17.0-1032-nvidia; driver 580.173.02; GB10.
Tailscale gx10-01 = 100.105.214.61. SSH legenex-02@gx10-02 works.
