#!/usr/bin/env bash
# Deploy the Control Center on gx10-01 and PROVE what the browser is served.
#
#   legenex/control-ui/scripts/deploy.sh          restart if needed, verify
#   legenex/control-ui/scripts/deploy.sh --verify verify only; change nothing
#   legenex/control-ui/scripts/deploy.sh --force  restart even when nothing looks stale
#
# Same contract as legenex/playground/scripts/deploy.sh (D-041). The server now
# re-reads a web/ file when it changes on disk, but the Python package is still
# loaded once, and a restart also applies any pending database migration (with a
# pre-migration copy written next to library.db). After the restart this script
# compares the ETag of every file the server actually serves with sha256 of the
# file in the checkout, and fails if they disagree.
set -euo pipefail
cd "$(dirname "$0")/.."
ui_dir="$(pwd)"
mode="${1:-}"
lock="/srv/projects/gx-cluster/state/build-v3/restart.lock"
base="http://127.0.0.1:8088"

step() { printf '\n== %s ==\n' "$*"; }
fail() { echo "DEPLOY FAILED: $*" >&2; exit 1; }

[ "$(hostname)" = "gx10-01" ] || fail "the Control Center runs on gx10-01 only (this is $(hostname))"

step "1/4 build validation"
node scripts/build-check.mjs

step "2/4 restart (only when the server code or the unit changed)"
started=$(systemctl --user show gx-control-ui.service -p ExecMainStartTimestampMonotonic --value)
now_s=$(awk '{print int($1)}' /proc/uptime)
start_epoch=$(( $(date +%s) - (now_s - started / 1000000) ))
newer=$(find gx_control_ui systemd -name '*.py' -o -name '*.sql' -o -name '*.service' 2>/dev/null \
        | xargs -r stat -c '%Y %n' | sort -rn | head -1 || true)
newest_epoch=${newer%% *}
need_restart=0
if [ "${mode}" = "--force" ]; then
  need_restart=1; reason="--force"
elif [ -n "${newest_epoch}" ] && [ "${newest_epoch}" -gt "${start_epoch}" ]; then
  need_restart=1; reason="${newer#* } is newer than the running process"
fi
pending=$(ls gx_control_ui/migrations/*.sql 2>/dev/null | wc -l)
if [ "${mode}" = "--verify" ]; then
  [ "${need_restart}" = "0" ] && echo "no restart needed" || echo "STALE: ${reason}"
elif [ "${need_restart}" = "1" ]; then
  echo "restarting: ${reason}"
  echo "note: a restart applies every pending migration (of ${pending} files on disk),"
  echo "      writing library.pre-<name>.db next to the database first."
  mkdir -p "$(dirname "${lock}")"
  exec 9>"${lock}"
  flock -w 180 9 || fail "could not take ${lock} within 180 s (another deploy is running)"
  systemctl --user restart gx-control-ui.service
  for _ in $(seq 1 60); do
    curl -fsS -m 2 "${base}/api/ready" >/dev/null 2>&1 && break
    sleep 1
  done
  flock -u 9
else
  echo "no restart needed (server code unchanged since $(date -d @${start_epoch} '+%H:%M:%S'))"
fi

step "3/4 health and applied migrations"
curl -fsS -m 10 "${base}/api/health" || fail "the Control Center does not answer on ${base}"
echo
python3 - <<'PY'
import sqlite3
db = "/srv/projects/gx-cluster/media/metadata/library.db"
try:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute("SELECT name FROM schema_migrations ORDER BY name").fetchall()
    print("applied migrations:", ", ".join(r[0] for r in rows) or "(none)")
except Exception as exc:  # noqa: BLE001
    print(f"could not read {db}: {exc}")
PY

step "4/4 served-bundle verification (ETag == sha256 of the file on disk)"
python3 - "${ui_dir}/web" "${base}" <<'PY'
import hashlib, pathlib, sys, urllib.error, urllib.request

root, base = pathlib.Path(sys.argv[1]), sys.argv[2]
checked = mismatched = missing = 0
bad = []
for p in sorted(root.rglob("*")):
    if not p.is_file() or p.name.startswith("."):
        continue
    rel = "/" + p.relative_to(root).as_posix()
    want = '"' + hashlib.sha256(p.read_bytes()).hexdigest()[:20] + '"'
    try:
        with urllib.request.urlopen(base + rel, timeout=10) as r:
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
