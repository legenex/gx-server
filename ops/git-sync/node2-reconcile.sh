#!/usr/bin/env bash
# ============================================================================
# node2-reconcile.sh — gx10-02 (pull-only production mirror): make the
# checkout byte-identical to origin/main. NEVER commits, NEVER pushes.
#
# Unexpected local state is configuration DRIFT, not a branch. Before it is
# discarded, evidence is written to ${GX_SYNC_LOG_DIR}/drift/<timestamp>/
# (mode 0700):
#   status.txt        git status, HEAD, origin/main
#   files.txt         affected paths with sha256 and size
#   tracked.patch     diff of tracked changes, with secret-like values masked
#   local-commits/    format-patch of any commits not on origin/main
#   untracked/        copies of untracked, non-ignored files (<= 1 MiB each)
#
# Ignored files (e.g. a local .env) are never touched: the clean step is
# `git clean -fd`, not `-fdx`. Machine-specific secrets belong under
# /srv/projects/gx-cluster/secrets anyway.
# ============================================================================
set -uo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GX_SYNC_TAG="node2-reconcile"
# shellcheck source=./common.sh
source "${here}/common.sh"

gxs_require_role mirror

exec 8>"${GX_SYNC_LOCK}"
if ! flock -w 30 8; then
  gxs_log "another reconcile is running; skipping"
  exit 0
fi

if [ ! -d "${GX_SYNC_REPO}/.git" ]; then
  gxs_log "FATAL: ${GX_SYNC_REPO} is not a git checkout"
  exit 1
fi

# Defence in depth: the mirror must be unable to push even by accident.
if [ "$(g remote get-url --push "${GX_SYNC_REMOTE}" 2>/dev/null)" != "DISABLED-gx10-02-is-pull-only" ]; then
  g remote set-url --push "${GX_SYNC_REMOTE}" DISABLED-gx10-02-is-pull-only
  gxs_log "re-applied disabled push URL on ${GX_SYNC_REMOTE}"
fi

if ! GIT_TERMINAL_PROMPT=0 timeout "${GX_SYNC_NET_TIMEOUT}" git -C "${GX_SYNC_REPO}" fetch --quiet --prune "${GX_SYNC_REMOTE}" "+refs/heads/${GX_SYNC_BRANCH}:refs/remotes/${GX_SYNC_REMOTE}/${GX_SYNC_BRANCH}" 2>>"${GX_SYNC_LOG_DIR}/fetch-errors.log"; then
  gxs_log "fetch failed; will retry on the next timer tick"
  exit 0
fi

target="$(g rev-parse "${GX_SYNC_REMOTE}/${GX_SYNC_BRANCH}")"
head="$(g rev-parse HEAD 2>/dev/null || echo none)"
branch="$(g symbolic-ref --quiet --short HEAD 2>/dev/null || echo DETACHED)"
dirty="$(g status --porcelain=v1 --untracked-files=all)"
local_commits="$(g rev-list "${target}..HEAD" 2>/dev/null | head -50)"

if [ "${head}" = "${target}" ] && [ -z "${dirty}" ] && [ "${branch}" = "${GX_SYNC_BRANCH}" ]; then
  exit 0   # already converged; stay quiet
fi

mask() {
  sed -E \
    -e 's/((ghp|gho|ghs|github_pat)_)[A-Za-z0-9_]+/\1***MASKED***/g' \
    -e 's/(sk-)[A-Za-z0-9_-]{8,}/\1***MASKED***/g' \
    -e 's/(hf_)[A-Za-z0-9]{8,}/\1***MASKED***/g' \
    -e 's/(tskey-)[A-Za-z0-9-]+/\1***MASKED***/g' \
    -e 's/(AKIA)[0-9A-Z]{16}/\1***MASKED***/g' \
    -e 's/((KEY|TOKEN|SECRET|PASSWORD|PASSWD|SALT)[A-Z_]*[[:space:]]*[:=][[:space:]]*)[^[:space:]]+/\1***MASKED***/Ig' \
    -e '/-----BEGIN [A-Z ]*PRIVATE KEY-----/,/-----END [A-Z ]*PRIVATE KEY-----/c\***PRIVATE KEY BLOCK MASKED***'
}

if [ -n "${dirty}" ] || [ -n "${local_commits}" ]; then
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  d="${GX_SYNC_LOG_DIR}/drift/${ts}"
  mkdir -p "${d}"; chmod 700 "${GX_SYNC_LOG_DIR}/drift" "${d}"
  {
    echo "host: $(hostname)"; echo "time: $(date -Is)"
    echo "branch: ${branch}"; echo "HEAD: ${head}"; echo "origin/${GX_SYNC_BRANCH}: ${target}"
    echo; echo "== git status =="; g status --porcelain=v1 --untracked-files=all
  } > "${d}/status.txt"
  {
    g ls-files -z -m -d -o --exclude-standard | while IFS= read -r -d '' p; do
      if [ -f "${GX_SYNC_REPO}/${p}" ]; then
        printf '%s  %s  %s\n' "$(sha256sum "${GX_SYNC_REPO}/${p}" | cut -d' ' -f1)" "$(stat -c %s "${GX_SYNC_REPO}/${p}")" "${p}"
      else
        printf '%s  %s  %s\n' "deleted" "-" "${p}"
      fi
    done
  } > "${d}/files.txt"
  { g diff --binary HEAD; } 2>/dev/null | mask > "${d}/tracked.patch"
  if [ -n "${local_commits}" ]; then
    mkdir -p "${d}/local-commits"
    g format-patch --quiet -o "${d}/local-commits" "${target}..HEAD" >/dev/null 2>&1 || true
    for f in "${d}"/local-commits/*.patch; do [ -f "$f" ] && mask < "$f" > "$f.m" && mv "$f.m" "$f"; done
  fi
  g ls-files -z -o --exclude-standard | while IFS= read -r -d '' p; do
    sz=$(stat -c %s "${GX_SYNC_REPO}/${p}" 2>/dev/null || echo 0)
    if [ "${sz}" -le 1048576 ]; then
      mkdir -p "$(dirname "${d}/untracked/${p}")"
      mask < "${GX_SYNC_REPO}/${p}" > "${d}/untracked/${p}" 2>/dev/null || true
    fi
  done
  chmod -R go-rwx "${d}"
  gxs_log "DRIFT on gx10-02: $(printf '%s\n' "${dirty}" | grep -c .) path(s), $(printf '%s\n' "${local_commits}" | grep -c .) local commit(s); evidence in ${d}; files: $(awk '{print $3}' "${d}/files.txt" | tr '\n' ' ')"
fi

if gxs_repo_busy; then
  gxs_log "repository busy (${GXS_BUSY_REASON}); aborting in-progress operation to restore the mirror"
  g merge --abort >/dev/null 2>&1; g rebase --abort >/dev/null 2>&1
  g cherry-pick --abort >/dev/null 2>&1; g revert --abort >/dev/null 2>&1; g bisect reset >/dev/null 2>&1
fi

if [ "${branch}" != "${GX_SYNC_BRANCH}" ]; then
  g checkout --quiet -B "${GX_SYNC_BRANCH}" "${target}" || { gxs_log "FATAL: could not check out ${GX_SYNC_BRANCH}"; exit 1; }
fi
g reset --quiet --hard "${target}" || { gxs_log "FATAL: reset to ${target} failed"; exit 1; }
g clean --quiet -fd
g branch --quiet --set-upstream-to="${GX_SYNC_REMOTE}/${GX_SYNC_BRANCH}" "${GX_SYNC_BRANCH}" >/dev/null 2>&1 || true

if [ "$(g rev-parse HEAD)" = "${target}" ] && [ -z "$(g status --porcelain=v1 --untracked-files=all)" ]; then
  gxs_log "reconciled: ${head:0:12} -> ${target:0:12}"
  printf '%s %s\n' "$(date -Is)" "${target}" > "${GX_SYNC_STATE_DIR}/last-reconciled"
else
  gxs_log "ERROR: checkout still differs from origin/${GX_SYNC_BRANCH} after reconcile"
  exit 1
fi
