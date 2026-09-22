#!/usr/bin/env bash
# Encrypted restic backup for the dual GX10 cluster.
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"

ROOT="${GX_BACKUP_ROOT:-$HOME/Documents/Projects/gx-backup}"
CONF="${GX_BACKUP_CONF:-$HOME/.config/gx-backup}"
PASS="$CONF/restic-password"
LOCAL_REPO="${GX_RESTIC_LOCAL:-$HOME/Documents/Backups/restic/local}"
PEER_PULL="${GX_RESTIC_PEER_PULL:-$HOME/Documents/Backups/restic/peer-gx10-02}"
PEER_PUSH="sftp:gx10-02:/home/legenex-02/Documents/Backups/restic/peer-gx10-01"
NODE2_LOCAL="sftp:gx10-02:/home/legenex-02/Documents/Backups/restic/local"
EXCLUDE="$ROOT/backup/exclude.txt"
HOST="$(hostname -s)"
export RESTIC_PASSWORD_FILE="$PASS"
export RESTIC_FROM_PASSWORD_FILE="$PASS"

die() { echo "ERROR: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null || die "missing $1"; }

need restic
[[ -f "$PASS" ]] || die "missing $PASS"
chmod 600 "$PASS" || true

init_repo() {
  local repo="$1"
  if restic -r "$repo" cat config >/dev/null 2>&1; then
    echo "restic repo exists: $repo"
  else
    echo "init restic repo: $repo"
    restic -r "$repo" init
  fi
}

cmd_init() {
  mkdir -p "$HOME/Documents/Backups/restic/local" "$HOME/Documents/Backups/restic/peer-gx10-02"
  init_repo "$LOCAL_REPO"
  ssh gx10-02 'mkdir -p ~/Documents/Backups/restic/local ~/Documents/Backups/restic/peer-gx10-01'
  # init remote repos using the same password
  if ! restic -r "$NODE2_LOCAL" cat config >/dev/null 2>&1; then
    restic -r "$NODE2_LOCAL" init
  fi
  if ! restic -r "$PEER_PUSH" cat config >/dev/null 2>&1; then
    restic -r "$PEER_PUSH" init
  fi
  if ! restic -r "$PEER_PULL" cat config >/dev/null 2>&1; then
    restic -r "$PEER_PULL" init
  fi
}

paths_node1() {
  cat <<'EOF'
/home/legenex
/home/legenex/.config
/home/legenex/.ssh
/home/legenex/.local/share
/opt/open-webui
/opt/gx-cluster
/srv/projects
/srv/logs
EOF
}

cmd_backup() {
  cmd_init
  local stamp dumps tag
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  dumps="$HOME/Documents/Backups/dumps/$stamp"
  echo "== database dumps =="
  GX_DUMP_STAMP="$stamp" bash "$ROOT/backup/dump-databases.sh" "$dumps" || echo "WARN: dump-databases had errors"

  echo "== package manifests =="
  mkdir -p "$ROOT/package-manifests"
  dpkg-query -W -f='${Package}\t${Version}\n' >"$ROOT/package-manifests/dpkg-gx10-01.txt" 2>/dev/null || true
  python3 -m pip freeze >"$ROOT/package-manifests/pip-user-gx10-01.txt" 2>/dev/null || true
  docker images --format '{{.Repository}}:{{.Tag}} {{.ID}} {{.Size}}' >"$ROOT/package-manifests/docker-images-gx10-01.txt" 2>/dev/null || true
  docker ps -a --format '{{.Names}} {{.Image}} {{.Status}}' >"$ROOT/package-manifests/docker-containers-gx10-01.txt" 2>/dev/null || true

  echo "== restic backup gx10-01 =="
  tag="gx10-01,$stamp"
  restic -r "$LOCAL_REPO" backup \
    --exclude-file "$EXCLUDE" \
    --tag "$tag" \
    --one-file-system \
    --hostname gx10-01 \
    /home/legenex \
    /opt/open-webui \
    /srv/projects \
    /srv/logs \
    "$dumps" \
    "$ROOT" \
    || true

  echo "== restic backup gx10-02 (via ssh) =="
  ssh gx10-02 "export PATH=\$HOME/.local/bin:\$PATH RESTIC_PASSWORD_FILE=\$HOME/.config/gx-backup/restic-password
    mkdir -p \$HOME/Documents/Backups/restic/local
    restic -r \$HOME/Documents/Backups/restic/local snapshots >/dev/null 2>&1 || restic -r \$HOME/Documents/Backups/restic/local init
    restic -r \$HOME/Documents/Backups/restic/local backup --one-file-system --hostname gx10-02 --tag gx10-02,$stamp \
      --exclude '.cache' --exclude '.npm' --exclude '.nvm' --exclude '.vscode-server' --exclude 'node_modules' \
      --exclude '/srv/models' --exclude '.nv' --exclude '/srv/cache' \
      --exclude '/srv/projects/gx-cluster/runtime' \
      \$HOME /srv/projects /srv/logs /srv/ai-stack" || echo "WARN: gx10-02 backup had errors"

  echo "== cross-node copy =="
  restic -r "$PEER_PULL" copy --from-repo "$NODE2_LOCAL" --from-password-file "$PASS" || echo "WARN: copy gx10-02 -> gx10-01 failed"
  restic -r "$PEER_PUSH" copy --from-repo "$LOCAL_REPO" --from-password-file "$PASS" || echo "WARN: copy gx10-01 -> gx10-02 failed"

  echo "== integrity =="
  restic -r "$LOCAL_REPO" check --read-data-subset=5%
  echo "$stamp" >"$HOME/Documents/Backups/LATEST"
  echo "snapshot $stamp"
}

cmd_snapshots() {
  echo "=== local (gx10-01) ==="
  restic -r "$LOCAL_REPO" snapshots
  echo "=== peer copy of gx10-02 ==="
  restic -r "$PEER_PULL" snapshots || true
}

cmd_verify() {
  restic -r "$LOCAL_REPO" check --read-data-subset="${1:-5%}"
}

cmd_restore_test() {
  local dest="${1:-/tmp/gx-restore-test}"
  mkdir -p "$dest"
  chmod 700 "$dest"
  restic -r "$LOCAL_REPO" restore latest --target "$dest" --include "$HOME/Documents/Projects/gx-backup/models.lock"
  echo "restored test files under $dest"
}

case "${1:-backup}" in
  init) cmd_init ;;
  backup) cmd_backup ;;
  snapshots) cmd_snapshots ;;
  verify) cmd_verify "${2:-5%}" ;;
  restore-test) cmd_restore_test "${2:-}" ;;
  *) die "usage: restic.sh init|backup|snapshots|verify|restore-test" ;;
esac
