#!/usr/bin/env bash
# ============================================================================
# node1-autosync.sh — gx10-01 (the only writer): commit legitimate source
# changes after a quiet period, push origin/main, and tell gx10-02 to
# reconcile. Safe to run concurrently from the watcher, the 1-minute timer
# and the post-commit hook: every mutating pass holds one flock.
#
#   node1-autosync.sh watch   persistent loop, polls every GX_SYNC_POLL_S (15s)
#   node1-autosync.sh once    one pass: commit if quiet, push if ahead, notify
#   node1-autosync.sh push    push if ahead and notify (post-commit hook)
#
# A pass NEVER commits when:
#   * this node's role is not "writer"
#   * the branch is not main, or HEAD is detached
#   * a merge/rebase/cherry-pick/revert/bisect is in progress or index.lock exists
#   * any changed file was modified less than GX_SYNC_QUIET_S seconds ago
#   * the staged diff contains a conflict marker
#   * the secret scan reports a finding (everything is unstaged, nothing committed)
# Forbidden paths (secrets, weights, archives, runtime state) and oversized
# files are unstaged and logged by NAME ONLY; the rest of the change proceeds.
#
# GitHub unreachable: the commit stays local, the failure is logged to
# ${GX_SYNC_LOG_DIR}/push-failures.log, and the next pass retries. Exit code
# is 0 in that case so a network blip never flaps a systemd unit.
# ============================================================================
set -uo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GX_SYNC_TAG="node1-autosync"
# shellcheck source=./common.sh
source "${here}/common.sh"

MODE="${1:-once}"
GX_SYNC_POLL_S="${GX_SYNC_POLL_S:-15}"

# Newest mtime (epoch) among changed paths; deleted paths count via their dir.
newest_change_epoch() {
  local newest=0 p t
  while IFS= read -r -d '' p; do
    if [ -e "${GX_SYNC_REPO}/${p}" ]; then
      t=$(stat -c %Y "${GX_SYNC_REPO}/${p}" 2>/dev/null || echo 0)
    else
      t=$(stat -c %Y "$(dirname "${GX_SYNC_REPO}/${p}")" 2>/dev/null || echo 0)
    fi
    [ "${t}" -gt "${newest}" ] && newest="${t}"
  done < <({ g ls-files -z -m -d -o --exclude-standard; g diff --cached --name-only -z; } 2>/dev/null)
  echo "${newest}"
}

has_changes() { [ -n "$(g status --porcelain=v1 --untracked-files=all)" ]; }

ahead_count() { g rev-list --count "${GX_SYNC_REMOTE}/${GX_SYNC_BRANCH}..HEAD" 2>/dev/null || echo 0; }

do_push() {
  local ahead out rc
  ahead="$(ahead_count)"
  # A remote-tracking ref that does not exist yet (first push) counts as ahead.
  g rev-parse --verify -q "${GX_SYNC_REMOTE}/${GX_SYNC_BRANCH}" >/dev/null || ahead=1
  [ "${ahead}" -gt 0 ] || return 0
  # Back off after a failure so a long GitHub outage costs one attempt per
  # GX_SYNC_PUSH_BACKOFF_S, not one per 15 s watcher poll. A manual commit
  # (MODE=push) always tries immediately.
  local stamp="${GX_SYNC_STATE_DIR}/last-push-failure"
  if [ "${MODE}" != push ] && [ -f "${stamp}" ] && \
     [ $(( $(date +%s) - $(stat -c %Y "${stamp}") )) -lt "${GX_SYNC_PUSH_BACKOFF_S:-60}" ]; then
    return 1
  fi
  out="$(GIT_TERMINAL_PROMPT=0 timeout "${GX_SYNC_NET_TIMEOUT}" git -C "${GX_SYNC_REPO}" push --quiet "${GX_SYNC_REMOTE}" "HEAD:refs/heads/${GX_SYNC_BRANCH}" 2>&1)"; rc=$?
  if [ "${rc}" -ne 0 ]; then
    printf '%s push FAILED rc=%s ahead=%s head=%s: %s\n' "$(date -Is)" "${rc}" "${ahead}" \
      "$(g rev-parse --short HEAD)" "$(printf '%s' "${out}" | tr '\n' ' ' | cut -c1-300)" \
      >> "${GX_SYNC_LOG_DIR}/push-failures.log"
    gxs_log "push failed (rc=${rc}); ${ahead} commit(s) kept locally, will retry"
    touch "${stamp}"
    return 1
  fi
  rm -f "${stamp}"
  g fetch --quiet "${GX_SYNC_REMOTE}" "${GX_SYNC_BRANCH}" >/dev/null 2>&1 || true
  gxs_log "pushed ${ahead} commit(s); origin/${GX_SYNC_BRANCH} = $(g rev-parse --short HEAD)"
  if gxs_notify_node2; then
    gxs_log "node2 reconcile triggered"
  else
    gxs_log "node2 notify failed; its 1-minute timer will converge it"
  fi
  return 0
}

do_commit() {
  local newest now branch rejected=() p hits
  has_changes || return 0
  branch="$(g symbolic-ref --quiet --short HEAD 2>/dev/null || echo DETACHED)"
  if [ "${branch}" != "${GX_SYNC_BRANCH}" ]; then
    gxs_log "skip: on '${branch}', autosync only commits on ${GX_SYNC_BRANCH}"
    return 0
  fi
  if gxs_repo_busy; then
    gxs_log "skip: repository busy (${GXS_BUSY_REASON})"
    return 0
  fi
  now=$(date +%s); newest="$(newest_change_epoch)"
  if [ $(( now - newest )) -lt "${GX_SYNC_QUIET_S}" ]; then
    return 0   # still being edited; debounce
  fi

  g add -A || { gxs_log "git add failed"; return 1; }

  while IFS= read -r -d '' p; do
    if gxs_forbidden_path "${p}" || gxs_file_too_large "${p}"; then
      rejected+=("${p}")
      g reset -q -- "${p}" >/dev/null 2>&1 || true
    fi
  done < <(g diff --cached --name-only -z --diff-filter=AMR)
  if [ "${#rejected[@]}" -gt 0 ]; then
    gxs_log "REJECTED (not committed, add to .gitignore or move out of the repo): ${rejected[*]}"
    printf '%s rejected: %s\n' "$(date -Is)" "${rejected[*]}" >> "${GX_SYNC_LOG_DIR}/rejected-paths.log"
  fi

  if g diff --cached --quiet; then
    return 0
  fi

  if g diff --cached --check 2>&1 | grep -q 'conflict marker'; then
    gxs_log "BLOCKED: staged change contains merge conflict markers; unstaging"
    g reset -q >/dev/null 2>&1
    return 1
  fi
  if ws="$(g diff --cached --check 2>&1)"; then :; else
    gxs_log "note: whitespace issues in staged change (not blocking): $(printf '%s' "${ws}" | grep -c .) line(s)"
  fi

  hits="$(gxs_scan_staged)"; case $? in
    0) ;;
    1) gxs_log "BLOCKED: secret scan found possible credentials; nothing committed. Findings (no values): $(printf '%s' "${hits}" | tr '\n' ' ')"
       printf '%s secret-scan block: %s\n' "$(date -Is)" "$(printf '%s' "${hits}" | tr '\n' ' ')" >> "${GX_SYNC_LOG_DIR}/secret-blocks.log"
       g reset -q >/dev/null 2>&1
       return 1 ;;
    *) gxs_log "BLOCKED: secret scanner unavailable; refusing to auto-commit to a public repository"
       g reset -q >/dev/null 2>&1
       return 1 ;;
  esac

  if GX_SYNC_IN_PROGRESS=1 g commit --quiet -m "autosync(gx10-01): $(date '+%Y-%m-%d %H:%M:%S')" \
       -m "Automatic commit of $(g diff --cached --name-only | grep -c .) file(s) by ops/git-sync/node1-autosync.sh" >/dev/null 2>&1; then
    gxs_log "committed $(g rev-parse --short HEAD): $(g show --stat --format= HEAD | tail -1)"
  else
    gxs_log "commit failed (hook refusal?); unstaging"
    g reset -q >/dev/null 2>&1
    return 1
  fi
}

pass() {
  gxs_require_role writer
  exec 8>"${GX_SYNC_LOCK}"
  if ! flock -n 8; then
    return 0   # another pass is running; it will do the work
  fi
  case "${MODE}" in
    push) do_push || true ;;
    *)    do_commit || true; do_push || true ;;
  esac
  flock -u 8
}

case "${MODE}" in
  once|push) pass ;;
  watch)
    gxs_require_role writer
    gxs_log "watcher started (poll ${GX_SYNC_POLL_S}s, quiet ${GX_SYNC_QUIET_S}s) on ${GX_SYNC_REPO}"
    while :; do
      ( pass ) || true
      sleep "${GX_SYNC_POLL_S}"
    done ;;
  *) echo "usage: $0 {watch|once|push}" >&2; exit 64 ;;
esac
