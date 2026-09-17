#!/usr/bin/env bash
# Deploy GX-Playground on gx10-01 and PROVE what the browser is served.
#
#   legenex/playground/scripts/deploy.sh          build if needed, restart if needed, verify
#   legenex/playground/scripts/deploy.sh --verify verify only; change nothing
#   legenex/playground/scripts/deploy.sh --force  restart even when nothing looks stale
#
# Why this exists (B-031). The Playground used to read web/ into memory once at
# start-up, so editing the source changed nothing the browser could see until
# somebody happened to restart the service. The server now re-reads a file when
# it changes on disk, but the Python package itself is still loaded once, and a
# built bundle still has to be rebuilt. This script is the single sanctioned
# deployment path: it rebuilds what needs rebuilding, restarts only when the
# Python package or the unit changed, and then compares the ETag of every file
# the server actually serves against sha256 of the file on disk. A mismatch
# fails the deploy instead of being discovered hours later in a browser.
set -euo pipefail
cd "$(dirname "$0")/.."
pg_dir="$(pwd)"
mode="${1:-}"
lock="/srv/projects/gx-cluster/state/build-v3/restart.lock"
base="http://127.0.0.1:8090"

step() { printf '\n== %s ==\n' "$*"; }
fail() { echo "DEPLOY FAILED: $*" >&2; exit 1; }

[ "$(hostname)" = "gx10-01" ] || fail "GX-Playground runs on gx10-01 only (this is $(hostname))"

# ---------------------------------------------------------------- 1. build
if [ "${mode}" != "--verify" ]; then
  step "1/4 build"
  if [ -f flows-ui/package.json ]; then
    if [ -d flows-ui/node_modules ]; then
      newest_src=$(find flows-ui/src flows-ui/package.json flows-ui/vite.config.ts -type f -newer web/flows/index.js 2>/dev/null | head -1 || true)
      if [ ! -f web/flows/index.js ] || [ -n "${newest_src}" ]; then
        echo "building flows-ui -> web/flows/"
        if ! (cd flows-ui && npm run build); then
          # The bundle is only required once Creative Flows is a shipped page.
          if [ -f web/js/pages/flows.js ]; then
            fail "the Creative Flows bundle did not build, but web/js/pages/flows.js ships it"
          fi
          echo "WARNING: flows-ui does not build yet and no page ships it; continuing"
        fi
      else
        echo "flows bundle up to date"
      fi
    else
      echo "flows-ui/node_modules missing; skipping the React bundle (run npm ci in flows-ui)"
    fi
  fi
  node scripts/build-check.mjs
fi

# -------------------------------------------------------------- 2. restart
step "2/4 restart (only when the server code or the unit changed)"
started=$(systemctl --user show gx-playground.service -p ExecMainStartTimestampMonotonic --value)
started_s=$(( started / 1000000 ))
now_s=$(awk '{print int($1)}' /proc/uptime)
age_s=$(( now_s - started_s ))
newer=$(find gx_playground systemd -name '*.py' -o -name '*.service' 2>/dev/null \
        | xargs -r stat -c '%Y %n' | sort -rn | head -1 || true)
newest_epoch=${newer%% *}
start_epoch=$(( $(date +%s) - age_s ))
need_restart=0
if [ "${mode}" = "--force" ]; then
  need_restart=1; reason="--force"
elif [ -n "${newest_epoch}" ] && [ "${newest_epoch}" -gt "${start_epoch}" ]; then
  need_restart=1; reason="${newer#* } is newer than the running process"
fi
if [ "${mode}" = "--verify" ]; then
  [ "${need_restart}" = "0" ] && echo "no restart needed" || echo "STALE: ${reason}"
elif [ "${need_restart}" = "1" ]; then
  echo "restarting: ${reason}"
  mkdir -p "$(dirname "${lock}")"
  # BUILD_V3 rule 9: hold the shared restart lock; Playground jobs live in memory.
  exec 9>"${lock}"
  flock -w 120 9 || fail "could not take ${lock} within 120 s (another deploy is running)"
  systemctl --user restart gx-playground.service
  for _ in $(seq 1 30); do
    curl -fsS -m 2 "${base}/pg/health" >/dev/null 2>&1 && break
    sleep 1
  done
  flock -u 9
else
  echo "no restart needed (server code unchanged since $(date -d @${start_epoch} '+%H:%M:%S'))"
fi

# --------------------------------------------------------------- 3. health
step "3/4 health"
health=$(curl -fsS -m 5 "${base}/pg/health") || fail "gx-playground does not answer on ${base}"
echo "${health}"
echo "${health}" | grep -q '"status": "ok"' || fail "unhealthy: ${health}"

# ------------------------------------------- 4. what the browser really gets
step "4/4 served-bundle verification (ETag == sha256 of the file on disk)"
python3 - "${pg_dir}/web" "${base}" <<'PY'
import hashlib, pathlib, sys, urllib.request, urllib.error

root, base = pathlib.Path(sys.argv[1]), sys.argv[2]
checked = mismatched = missing = 0
bad = []
for p in sorted(root.rglob("*")):
    if not p.is_file() or p.name.startswith("."):
        continue
    rel = "/" + p.relative_to(root).as_posix()
    want = '"' + hashlib.sha256(p.read_bytes()).hexdigest()[:20] + '"'
    req = urllib.request.Request(base + rel)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            got = r.headers.get("ETag")
    except urllib.error.HTTPError as exc:
        missing += 1
        bad.append(f"  {rel}: HTTP {exc.code} (on disk but not served)")
        continue
    checked += 1
    if got != want:
        mismatched += 1
        bad.append(f"  {rel}: served {got} but the file on disk is {want}")
print(f"{checked} files served match the checkout; {mismatched} stale, {missing} not served")
for line in bad[:25]:
    print(line)
sys.exit(1 if (mismatched or missing) else 0)
PY
printf '\nDEPLOY OK — the browser is being served this checkout\n'
