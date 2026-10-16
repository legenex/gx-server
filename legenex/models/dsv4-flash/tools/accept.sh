#!/usr/bin/env bash
# accept.sh — V4 Flash post-switch acceptance (run on gx10-01 after the switch SUCCEEDED).
#
#   GX_BENCH_KEY=<a LiteLLM key, never printed> legenex/models/dsv4-flash/tools/accept.sh [OUTDIR]
#
# Checks, each printed as PASS/FAIL, overall exit 0 only if all pass:
#   1 overlay   per-rank GX_WOB_DIGEST lines == the overlay bytes (not the base copy)
#   2 refusal   refusal_check.py PASS on gx-fast, gx-high, gx-max
#   3 bench     gx_bench c=1,2,8 on gx-fast: no errors, tool call + continuation OK
#   4 memory    MemAvailable >= 15 GiB on BOTH nodes for the whole 8-way run
# Read-only except for the requests themselves. Does not switch, stop or restart anything.
set -uo pipefail
KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-/srv/logs/acceptance/dsv4-flash-$(date -u +%Y%m%dT%H%M%SZ)}"
WORKER="${DSV4F_WORKER_SSH:-legenex-02@10.60.21.41}"
MODEL_DIR=/srv/models/dsv4-flash-0731-abliterated/21bd923c
FLOOR="${DSV4F_RESIDENT_FLOOR_GIB:-15}"
mkdir -p "$OUT"; chmod 700 "$OUT"
[ -n "${GX_BENCH_KEY:-}" ] || { echo "set GX_BENCH_KEY" >&2; exit 2; }
fails=0
result() { printf '%-8s %s  %s\n' "$1" "$2" "$3" | tee -a "$OUT/summary.txt"; [ "$2" = PASS ] || fails=$((fails+1)); }

# 1 overlay digests
docker logs dsv4f-head 2>&1 | grep GX_WOB_DIGEST > "$OUT/wob-head.log"
ssh -o BatchMode=yes "$WORKER" 'docker logs dsv4f-worker 2>&1 | grep GX_WOB_DIGEST' > "$OUT/wob-worker.log"
scp -q "$OUT/wob-head.log" "$OUT/wob-worker.log" "$WORKER:.cache/gx-dsv4-flash/run/"
if ssh -o BatchMode=yes "$WORKER" "python3 -I ~/.cache/gx-dsv4-flash/kit/tools/overlay_check.py compare $MODEL_DIR \
      ~/.cache/gx-dsv4-flash/run/wob-head.log ~/.cache/gx-dsv4-flash/run/wob-worker.log" > "$OUT/overlay.json"; then
    result overlay PASS "every rank holds the overlay wo_b bytes"
else
    result overlay FAIL "$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1])).get("verdict"))' "$OUT/overlay.json" 2>/dev/null)"
fi

# 2 refusal
if GX_GATEWAY_KEY="$GX_BENCH_KEY" python3 -I "$KIT/tools/refusal_check.py" --out "$OUT/refusal.json" > "$OUT/refusal.log" 2>&1; then
    result refusal PASS "$(tail -2 "$OUT/refusal.log" | head -1)"
else
    result refusal FAIL "$(tail -2 "$OUT/refusal.log" | head -1)"
fi

# 3+4 bench with the memory watcher
bash "$KIT/tools/mem_watch.sh" "$OUT/mem.tsv" 5 "$WORKER" & mw=$!
python3 -I "$KIT/tools/bench.py" --model gx-fast --concurrency 1,2,8 --max-tokens 1024 > "$OUT/bench.log" 2>&1
brc=$?
kill -TERM "$mw"; wait "$mw" 2>/dev/null
python3 -I - "$OUT/bench.log" <<'PY' && result bench PASS "$(grep -o '"aggregate_tok_s": [0-9.]*' "$OUT/bench.log" | tr '\n' ' ')" || result bench FAIL "see $OUT/bench.log"
import json, sys
rep = [l for l in open(sys.argv[1]) if l.startswith("REPORT ")]
if not rep: sys.exit(1)
r = json.loads(rep[-1][7:])
ok = all(not run["errors"] and run["ok"] == run["concurrency"] for run in r["runs"])
tools = r.get("tools") or {}
sys.exit(0 if ok and tools.get("tool_call") and tools.get("continuation") else 1)
PY
[ "$brc" = 0 ] || result bench FAIL "bench.py exit $brc"
minline=$(awk -F'\t' 'NR==1||$2<h{h=$2} NR==1||$3<w{w=$3} END{print h, w}' "$OUT/mem.tsv")
set -- $minline
if awk -v h="$1" -v w="$2" -v f="$FLOOR" 'BEGIN{exit !(h>=f && w>=f)}'; then
    result memory PASS "min MemAvailable head=$1 worker=$2 GiB during c=8 (floor $FLOOR)"
else
    result memory FAIL "min MemAvailable head=$1 worker=$2 GiB during c=8 (floor $FLOOR)"
fi
echo "results: $OUT/summary.txt"
[ "$fails" = 0 ] && echo "ACCEPTANCE: GO" || echo "ACCEPTANCE: NO-GO ($fails failed)"
[ "$fails" = 0 ]
