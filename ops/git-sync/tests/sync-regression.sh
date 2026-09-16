#!/usr/bin/env bash
# ============================================================================
# sync-regression.sh — hermetic regression tests for the Git sync failure paths.
#
# Runs the REAL node1-autosync.sh and node2-reconcile.sh against throwaway
# repositories in a temp dir: a local bare repo stands in for GitHub, an ssh
# shim stands in for gx10-02. Nothing touches the real checkout, the real
# state/log directories, GitHub or gx10-02. Fake credentials are generated at
# run time, so no credential-shaped literal exists in this file.
#
#   S1  writer commits a quiet change and pushes it; node 2 is notified
#   S2  secret gate (gitleaks): nothing committed, finding logged without value
#   S3  secret gate (regex fallback when gitleaks is unavailable)
#   S4  forbidden paths are unstaged and logged; the rest is committed
#   S5  GitHub unreachable: commit kept locally, failure logged, exit 0;
#       the next pass after recovery pushes it
#   S6  a non-writer role refuses to act
#   S7  node 2 drift: evidence saved (secrets masked), mirror reset to origin,
#       push URL re-disabled
#   S8  node 2 fetch failure: exit 0, checkout untouched
#   S9  staged conflict markers are refused
#
# Usage: ops/git-sync/tests/sync-regression.sh     Exit code = failures.
# ============================================================================
set -uo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d)"
trap 'rm -rf "${T}"' EXIT
PASS=0; FAIL=0
pass(){ PASS=$((PASS+1)); printf '  PASS  %s\n' "$1"; }
fail(){ FAIL=$((FAIL+1)); printf '  FAIL  %s :: %s\n' "$1" "$2"; }

fake_token() { # fake_token <prefix> <len>
  python3 -c "import secrets,string,sys; a=string.ascii_letters+string.digits; print(sys.argv[1]+''.join(secrets.choice(a) for _ in range(int(sys.argv[2]))))" "$1" "$2"
}

# ------------------------------------------------------------------- fixture
mkdir -p "${T}/bin" "${T}/w-state" "${T}/w-logs" "${T}/m-state" "${T}/m-logs"
cat > "${T}/bin/ssh" <<EOF
#!/usr/bin/env bash
echo "ssh \$*" >> "${T}/ssh-calls.log"
exit 0
EOF
chmod +x "${T}/bin/ssh"
export PATH="${T}/bin:${PATH}"
export GIT_CONFIG_GLOBAL="${T}/gitconfig"
git config --global user.name "sync-regression"
git config --global user.email "sync-regression@invalid"
git config --global init.defaultBranch main
git config --global advice.detachedHead false

git init -q --bare "${T}/origin.git"
git init -q "${T}/seed"
printf '.env\n*.env\n!*.env.sample\n' > "${T}/seed/.gitignore"
echo "hello" > "${T}/seed/README.md"
git -C "${T}/seed" add -A && git -C "${T}/seed" commit -qm init
git -C "${T}/seed" push -q "${T}/origin.git" main
git clone -q "${T}/origin.git" "${T}/writer"
git clone -q "${T}/origin.git" "${T}/mirror"
git -C "${T}/mirror" remote set-url --push origin DISABLED-gx10-02-is-pull-only
echo writer > "${T}/w-state/role"
echo mirror > "${T}/m-state/role"

writer() { # writer <mode>
  GX_SYNC_REPO="${T}/writer" GX_SYNC_STATE_DIR="${T}/w-state" GX_SYNC_LOG_DIR="${T}/w-logs" \
  GX_SYNC_QUIET_S=0 GX_SYNC_PUSH_BACKOFF_S=0 GX_SYNC_NET_TIMEOUT=10 GX_SYNC_NODE2_SSH=fake-node2 \
    bash "${here}/node1-autosync.sh" "$@" >/dev/null 2>&1
}
mirror() {
  GX_SYNC_REPO="${T}/mirror" GX_SYNC_STATE_DIR="${T}/m-state" GX_SYNC_LOG_DIR="${T}/m-logs" \
  GX_SYNC_NET_TIMEOUT=10 bash "${here}/node2-reconcile.sh" >/dev/null 2>&1
}
origin_head() { git --git-dir="${T}/origin.git" rev-parse main; }
w() { git -C "${T}/writer" "$@"; }

echo "[git-sync regression]"

# S1 --------------------------------------------------------------------------
echo "feature" > "${T}/writer/feature.txt"
writer once
if [ "$(origin_head)" = "$(w rev-parse HEAD)" ] && w log -1 --format=%s | grep -q '^autosync(gx10-01)'; then
  pass "S1 quiet change committed and pushed"
else
  fail "S1" "origin=$(origin_head) head=$(w rev-parse HEAD)"
fi
grep -q 'gx-git-reconcile.service' "${T}/ssh-calls.log" 2>/dev/null \
  && pass "S1 node 2 reconcile notified" || fail "S1 notify" "no ssh call recorded"

# S2 --------------------------------------------------------------------------
if command -v gitleaks >/dev/null 2>&1 || [ -x "${HOME}/.local/bin/gitleaks" ]; then
  tok="$(fake_token ghp_ 36)"
  before="$(w rev-parse HEAD)"
  printf 'github_token = "%s"\n' "${tok}" > "${T}/writer/leak.cfg"
  writer once
  if [ "$(w rev-parse HEAD)" = "${before}" ] && [ -z "$(w diff --cached --name-only)" ]; then
    pass "S2 gitleaks gate: nothing committed, nothing left staged"
  else
    fail "S2" "a commit or staged change exists"
  fi
  if grep -q 'leak.cfg' "${T}/w-logs/secret-blocks.log" 2>/dev/null && ! grep -rq "${tok}" "${T}/w-logs"; then
    pass "S2 finding logged by file name, value never logged"
  else
    fail "S2 log" "secret-blocks.log missing the file or contains the value"
  fi
  rm -f "${T}/writer/leak.cfg"
else
  fail "S2" "gitleaks not installed (the production gate requires it)"
fi

# S3 --------------------------------------------------------------------------
tok="$(fake_token sk- 40)"
before="$(w rev-parse HEAD)"
printf 'key: %s\n' "${tok}" > "${T}/writer/leak2.txt"
GX_SYNC_GITLEAKS=/nonexistent/gitleaks writer once
if [ "$(w rev-parse HEAD)" = "${before}" ] && grep -q 'leak2.txt' "${T}/w-logs/secret-blocks.log" \
   && ! grep -rq "${tok}" "${T}/w-logs"; then
  pass "S3 regex fallback blocks the commit and logs no value"
else
  fail "S3" "fallback scan did not block, or leaked the value"
fi
rm -f "${T}/writer/leak2.txt"

# S4 --------------------------------------------------------------------------
mkdir -p "${T}/writer/models" "${T}/writer/sub/.state"
echo "secret=1" > "${T}/writer/prod.env"
echo "w" > "${T}/writer/models/weights.safetensors"
echo "{}" > "${T}/writer/sub/.state/x.json"
echo "ok" > "${T}/writer/legit.md"
git -C "${T}/writer" add -f prod.env >/dev/null 2>&1 || true   # even a forced add must be refused
writer once
tracked="$(w ls-files)"
if printf '%s\n' "${tracked}" | grep -qx legit.md && ! printf '%s\n' "${tracked}" | grep -qE 'prod.env|safetensors|\.state/'; then
  pass "S4 forbidden paths refused, legitimate file committed"
else
  fail "S4" "tracked: $(printf '%s' "${tracked}" | tr '\n' ' ')"
fi
grep -q 'weights.safetensors' "${T}/w-logs/rejected-paths.log" 2>/dev/null \
  && pass "S4 rejected paths logged by name" || fail "S4 log" "rejected-paths.log missing entries"
rm -rf "${T}/writer/models" "${T}/writer/sub" "${T}/writer/prod.env"

# S5 --------------------------------------------------------------------------
w remote set-url origin "${T}/github-is-down.git"
echo "offline edit" > "${T}/writer/offline.txt"
writer once; rc=$?
if [ "${rc}" -eq 0 ] && [ "$(w rev-parse HEAD)" != "$(origin_head)" ] && w log -1 --name-only | grep -q offline.txt; then
  pass "S5 GitHub down: commit kept locally, exit 0"
else
  fail "S5" "rc=${rc}"
fi
grep -q 'push FAILED' "${T}/w-logs/push-failures.log" 2>/dev/null \
  && pass "S5 push failure logged" || fail "S5 log" "push-failures.log has no entry"
w remote set-url origin "${T}/origin.git"
writer once
[ "$(w rev-parse HEAD)" = "$(origin_head)" ] && pass "S5 recovery: next pass pushes the kept commit" \
  || fail "S5 recovery" "origin not updated"

# S6 --------------------------------------------------------------------------
echo mirror > "${T}/w-state/role"
echo "should not commit" > "${T}/writer/nope.txt"
before="$(w rev-parse HEAD)"
GX_SYNC_REPO="${T}/writer" GX_SYNC_STATE_DIR="${T}/w-state" GX_SYNC_LOG_DIR="${T}/w-logs" GX_SYNC_QUIET_S=0 \
  bash "${here}/node1-autosync.sh" once >/dev/null 2>&1; rc=$?
if [ "${rc}" -eq 64 ] && [ "$(w rev-parse HEAD)" = "${before}" ]; then
  pass "S6 non-writer role refused (exit 64), nothing committed"
else
  fail "S6" "rc=${rc}"
fi
echo writer > "${T}/w-state/role"
rm -f "${T}/writer/nope.txt"

# S7 --------------------------------------------------------------------------
m() { git -C "${T}/mirror" "$@"; }
m fetch -q && m reset -q --hard origin/main
tok="$(fake_token hf_ 34)"
echo "drifted" >> "${T}/mirror/README.md"
printf 'TOKEN=%s\n' "${tok}" > "${T}/mirror/untracked.conf"
echo "local" > "${T}/mirror/local.txt" && m add local.txt && m commit -qm "local commit on mirror"
echo "keepme" > "${T}/mirror/node.env"       # ignored: must survive
m remote set-url --push origin "${T}/origin.git"   # tampered push URL
mirror
d="$(ls -d "${T}"/m-logs/drift/* 2>/dev/null | tail -1)"
if [ "$(m rev-parse HEAD)" = "$(origin_head)" ] && [ -z "$(m status --porcelain)" ]; then
  pass "S7 mirror reset to origin/main and clean"
else
  fail "S7" "HEAD=$(m rev-parse HEAD) status=$(m status --porcelain | tr '\n' ' ')"
fi
if [ -n "${d}" ] && [ -f "${d}/files.txt" ] && [ -f "${d}/tracked.patch" ] && ls "${d}/local-commits/"*.patch >/dev/null 2>&1 \
   && [ -f "${d}/untracked/untracked.conf" ] && [ "$(stat -c %a "${d}")" = 700 ]; then
  pass "S7 drift evidence saved (status, files, patch, local commits, untracked; mode 0700)"
else
  fail "S7 evidence" "dir=${d:-none}"
fi
if [ -n "${d}" ] && ! grep -rq "${tok}" "${d}"; then
  pass "S7 secret-like values masked in the evidence"
else
  fail "S7 masking" "token found in evidence"
fi
[ -f "${T}/mirror/node.env" ] && pass "S7 ignored local files kept" || fail "S7 ignored" "node.env deleted"
[ "$(m remote get-url --push origin)" = "DISABLED-gx10-02-is-pull-only" ] \
  && pass "S7 push URL re-disabled" || fail "S7 push url" "$(m remote get-url --push origin)"

# S8 --------------------------------------------------------------------------
m remote set-url origin "${T}/github-is-down.git"
echo "local edit" >> "${T}/mirror/README.md"
mirror; rc=$?
if [ "${rc}" -eq 0 ] && m status --porcelain | grep -q README.md; then
  pass "S8 fetch failure: exit 0, checkout untouched until GitHub is back"
else
  fail "S8" "rc=${rc}"
fi
m remote set-url origin "${T}/origin.git"
mirror
[ -z "$(m status --porcelain)" ] && pass "S8 recovery: next reconcile converges" || fail "S8 recovery" "still dirty"

# S9 --------------------------------------------------------------------------
before="$(w rev-parse HEAD)"
printf '<<<<<<< HEAD\na\n=======\nb\n>>>>>>> other\n' > "${T}/writer/conflict.txt"
writer once
if [ "$(w rev-parse HEAD)" = "${before}" ] && [ -z "$(w diff --cached --name-only)" ]; then
  pass "S9 conflict markers refused"
else
  fail "S9" "conflict markers were committed"
fi

printf '\n PASS=%d FAIL=%d\n' "${PASS}" "${FAIL}"
exit "${FAIL}"
