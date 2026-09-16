#!/usr/bin/env bash
# ============================================================================
# ops/git-sync/common.sh — shared config and helpers for gx-cluster Git sync.
#
# Source-control model (D-026):
#   gx10-01  = the ONLY writer. Auto-commits and pushes origin/main.
#   GitHub   = legenex/gx-server, canonical remote and off-machine backup.
#   gx10-02  = pull-only production mirror, reconciled to origin/main.
#
# The node's role is read from ${GX_SYNC_ROLE_FILE} (written by
# install-sync.sh), never guessed from the hostname alone, and every script
# refuses to act outside its role.
# ============================================================================

GX_SYNC_REPO="${GX_SYNC_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
GX_SYNC_REMOTE="${GX_SYNC_REMOTE:-origin}"
GX_SYNC_BRANCH="${GX_SYNC_BRANCH:-main}"
GX_SYNC_PUBLIC_URL="${GX_SYNC_PUBLIC_URL:-https://github.com/legenex/gx-server.git}"
GX_SYNC_LOG_DIR="${GX_SYNC_LOG_DIR:-/srv/logs/gx-git-sync}"
GX_SYNC_STATE_DIR="${GX_SYNC_STATE_DIR:-/srv/projects/gx-cluster/state/git-sync}"
GX_SYNC_ROLE_FILE="${GX_SYNC_ROLE_FILE:-${GX_SYNC_STATE_DIR}/role}"
GX_SYNC_LOCK="${GX_SYNC_LOCK:-${GX_SYNC_STATE_DIR}/sync.lock}"
# Seconds a change must sit untouched before it is committed.
GX_SYNC_QUIET_S="${GX_SYNC_QUIET_S:-45}"
GX_SYNC_NODE2_SSH="${GX_SYNC_NODE2_SSH:-legenex-02@gx10-02}"
GX_SYNC_NET_TIMEOUT="${GX_SYNC_NET_TIMEOUT:-60}"
GX_SYNC_GITLEAKS="${GX_SYNC_GITLEAKS:-$(command -v gitleaks 2>/dev/null || echo "${HOME}/.local/bin/gitleaks")}"
# Tracked files larger than this are refused at staging time.
GX_SYNC_MAX_FILE_BYTES="${GX_SYNC_MAX_FILE_BYTES:-5242880}"
# Pre-existing large tracked files that are known and accepted.
GX_SYNC_LARGE_ALLOW="${GX_SYNC_LARGE_ALLOW:-data/benchmarks.sqlite}"

mkdir -p "${GX_SYNC_LOG_DIR}" "${GX_SYNC_STATE_DIR}" 2>/dev/null || true
chmod 700 "${GX_SYNC_STATE_DIR}" 2>/dev/null || true

gxs_log() {
  local tag="${GX_SYNC_TAG:-git-sync}"
  printf '%s %s[%s] %s\n' "$(date -Is)" "${tag}" "$$" "$*" | tee -a "${GX_SYNC_LOG_DIR}/${tag}.log" >&2
}

gxs_role() { cat "${GX_SYNC_ROLE_FILE}" 2>/dev/null || echo unset; }

gxs_require_role() {
  local want="$1" have
  have="$(gxs_role)"
  if [ "${have}" != "${want}" ]; then
    gxs_log "REFUSED: this node's git-sync role is '${have}', this action needs '${want}'"
    exit 64
  fi
}

g() { git -C "${GX_SYNC_REPO}" "$@"; }

# 0 when the repository is in the middle of something a robot must not touch.
gxs_repo_busy() {
  local gd
  gd="$(g rev-parse --absolute-git-dir 2>/dev/null)" || return 0
  for f in MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD BISECT_LOG rebase-merge rebase-apply index.lock; do
    if [ -e "${gd}/${f}" ]; then
      GXS_BUSY_REASON="${f} present"
      return 0
    fi
  done
  return 1
}

# Paths that must never be committed, whatever .gitignore says.
gxs_forbidden_path() {
  local p="$1" base
  base="${p##*/}"
  case "${base}" in
    *.env.sample|*.env.example|*.env.template) return 1 ;;
  esac
  case "${p}" in
    legenex/gateway/llama-swap/env/*.env) return 1 ;;
  esac
  case "${base}" in
    .env|.env.*|*.env|*.pem|*.key|*.p12|*.pfx|id_rsa*|id_ed25519*|id_ecdsa*|.git-credentials|.netrc|*.token) return 0 ;;
    *.gguf|*.safetensors|*.ckpt|*.pt|*.pth|*.onnx|*.bundle|*.tar|*.tar.gz|*.tgz|*.zip|*.7z|swapfile*) return 0 ;;
    *-residency.json|*.pid) return 0 ;;
  esac
  case "/${p}/" in
    */.state/*|*/secrets/*|*/.secrets/*|*/node_modules/*|*/__pycache__/*) return 0 ;;
  esac
  return 1
}

gxs_file_too_large() {
  local p="$1" size
  [ -f "${GX_SYNC_REPO}/${p}" ] || return 1
  case " ${GX_SYNC_LARGE_ALLOW} " in *" ${p} "*) return 1 ;; esac
  size=$(stat -c %s "${GX_SYNC_REPO}/${p}" 2>/dev/null || echo 0)
  [ "${size}" -gt "${GX_SYNC_MAX_FILE_BYTES}" ]
}

# Secret scan of what is currently staged. Prints only file:line:rule, never
# the matched value. Returns 0 clean, 1 findings, 2 scanner unavailable.
gxs_scan_staged() {
  local report rc
  if [ -x "${GX_SYNC_GITLEAKS}" ]; then
    report="$(mktemp)"
    ( cd "${GX_SYNC_REPO}" && "${GX_SYNC_GITLEAKS}" git --pre-commit --staged --redact --no-banner \
        --log-level error --exit-code 1 --report-format json --report-path "${report}" . ) >/dev/null 2>&1
    rc=$?
    if [ "${rc}" -eq 1 ]; then
      python3 - "${report}" <<'PY' 2>/dev/null || echo "(gitleaks findings; report unreadable)"
import json, sys
for f in json.load(open(sys.argv[1])):
    print(f"{f.get('File')}:{f.get('StartLine')}:{f.get('RuleID')}")
PY
      rm -f "${report}"
      return 1
    fi
    rm -f "${report}"
    [ "${rc}" -eq 0 ] && return 0
  fi
  # Fallback: conservative regex scan of the staged diff (added lines only).
  local hits
  hits="$(g diff --cached -U0 --no-color | awk '
    /^\+\+\+ b\//{file=substr($0,7); next}
    /^\+/{ if ($0 ~ /(ghp_|gho_|ghs_|github_pat_)[A-Za-z0-9_]{20,}|tskey-[A-Za-z0-9-]{10,}|hf_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|sk-[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}/) print file ": secret-like token" }')"
  if [ -n "${hits}" ]; then printf '%s\n' "${hits}"; return 1; fi
  [ -x "${GX_SYNC_GITLEAKS}" ] && return 2
  return 0
}

# Ask node 2 to reconcile now. Never blocks the writer for long.
gxs_notify_node2() {
  timeout 25 ssh -o BatchMode=yes -o ConnectTimeout=10 "${GX_SYNC_NODE2_SSH}" \
    'systemctl --user start --no-block gx-git-reconcile.service' >/dev/null 2>&1
}
