#!/usr/bin/env bash
# accept.sh — V4 Flash post-switch acceptance (run on gx10-01 after the switch SUCCEEDED).
#
#   legenex/models/dsv4-flash/tools/accept.sh [OUTDIR]
#
# The gateway key is READ FROM A FILE (never argv, never shell history):
# $GX_BENCH_KEY_FILE (first line), else LITELLM_MASTER_KEY from the gateway
# .env (the secrets store behind the symlink).
#
# Checks, each printed as PASS/FAIL; overall exit 0 only if all pass:
#   1 overlay   PRIMARY proof of the abliterated weights: every rank's loaded
#               layers.0 wo_b == the overlay bytes (not the base copy)
#   2 refusal   refusal_check.py on gx-fast, gx-high, gx-max (corroboration)
#   3 bench     bench.py c=1,2,8 on gx-fast: all complete, tokens > 0, no raw
#               <think> in content, tool call + continuation
#   4 memory    MemAvailable >= 15 GiB on BOTH nodes for the whole run, no NA
#               reading, >= 10 samples
#   5 ranks     no rank container restarted (RestartCount + StartedAt equal at
#               the start and the end; both running)
# Read-only except for the requests themselves. Switches, stops, restarts nothing.
set -uo pipefail
KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="$(cd "$KIT/../../.." && pwd)"
OUT="${1:-/srv/logs/acceptance/dsv4-flash-$(date -u +%Y%m%dT%H%M%SZ)}"
WORKER="${DSV4F_WORKER_SSH:-legenex-02@10.60.21.41}"
MODEL_DIR=/srv/models/dsv4-flash-0731-abliterated/21bd923c
FLOOR="${DSV4F_RESIDENT_FLOOR_GIB:-15}"
EVAL="python3 -I $KIT/tools/accept_eval.py"
GATEWAY="${DSV4F_GATEWAY_URL:-http://127.0.0.1:4000/v1}"
mkdir -p "$OUT"; chmod 700 "$OUT"

mw=""
cleanup() { [ -n "$mw" ] && kill -TERM "$mw" 2>/dev/null; wait "$mw" 2>/dev/null; mw=""; }
trap cleanup EXIT
trap 'cleanup; echo "ACCEPTANCE: NO-GO (interrupted)"; exit 130' INT TERM

if [ -n "${GX_BENCH_KEY_FILE:-}" ]; then
    key="$(head -n1 "$GX_BENCH_KEY_FILE" | tr -d '\r\n')"
else
    key="$(sed -n 's/^LITELLM_MASTER_KEY=//p' "${GX_GATEWAY_ENV:-$REPO/legenex/gateway/.env}" | tail -1)"
fi
[ -n "$key" ] || { echo "no gateway key (GX_BENCH_KEY_FILE or LITELLM_MASTER_KEY in the gateway .env)"; echo "ACCEPTANCE: NO-GO"; exit 2; }
export GX_BENCH_KEY="$key" GX_GATEWAY_KEY="$key"   # environment of our own children only
unset key

fails=0
result() { printf '%-8s %s  %s\n' "$1" "$2" "$3" | tee -a "$OUT/summary.txt"; [ "$2" = PASS ] || fails=$((fails+1)); }
rank_state() {
    printf 'node1 %s\n' "$(docker inspect -f '{{.RestartCount}} {{.State.StartedAt}} {{.State.Running}}' dsv4f-head 2>/dev/null || echo 'NA NA false')"
    printf 'node2 %s\n' "$(ssh -o BatchMode=yes -o ConnectTimeout=10 "$WORKER" "docker inspect -f '{{.RestartCount}} {{.State.StartedAt}} {{.State.Running}}' dsv4f-worker" 2>/dev/null || echo 'NA NA false')"
}
rank_state > "$OUT/ranks-before.txt"
bash "$KIT/tools/mem_watch.sh" "$OUT/mem.tsv" 5 "$WORKER" & mw=$!

# 1 overlay digests (primary proof)
docker logs dsv4f-head 2>&1 | grep GX_WOB_DIGEST > "$OUT/wob-head.log"
ssh -o BatchMode=yes "$WORKER" 'docker logs dsv4f-worker 2>&1 | grep GX_WOB_DIGEST' > "$OUT/wob-worker.log"
scp -q "$OUT/wob-head.log" "$OUT/wob-worker.log" "$WORKER:.cache/gx-dsv4-flash/run/"
if ssh -o BatchMode=yes "$WORKER" "python3 -I ~/.cache/gx-dsv4-flash/kit/tools/overlay_check.py compare $MODEL_DIR \
      ~/.cache/gx-dsv4-flash/run/wob-head.log ~/.cache/gx-dsv4-flash/run/wob-worker.log" > "$OUT/overlay.json"; then
    result overlay PASS "every rank holds the overlay wo_b bytes"
else
    result overlay FAIL "$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1])).get("verdict"))' "$OUT/overlay.json" 2>/dev/null || echo "no digest result")"
fi

# 2 refusal (corroboration; matches are printed for manual review)
if python3 -I "$KIT/tools/refusal_check.py" --base "$GATEWAY" --out "$OUT/refusal.json" > "$OUT/refusal.log" 2>&1; then
    result refusal PASS "$(grep -E '^\{' "$OUT/refusal.log" | tail -1)"
else
    result refusal FAIL "$(grep -E '^\{' "$OUT/refusal.log" | tail -1) (matches: $OUT/refusal.log)"
fi

# 3 + 4 bench under the memory watcher
python3 -I "$KIT/tools/bench.py" --url "$GATEWAY/chat/completions" --model gx-fast --concurrency 1,2,4,6,8 --max-tokens 1024 > "$OUT/bench.log" 2>&1
brc=$?
if msg="$($EVAL bench "$OUT/bench.log")" && [ "$brc" = 0 ]; then result bench PASS "$msg"
else result bench FAIL "${msg:-bench.py exit $brc} (exit $brc)"; fi

# Every logical mode must stream, call a structured tool, and continue after
# its result. The switch validator covers identity and the physical fabric.
for alias in gx-auto gx-medium gx-high gx-max; do
    if python3 -I "$KIT/tools/bench.py" --url "$GATEWAY/chat/completions" --model "$alias" --concurrency 1 --max-tokens 2048 > "$OUT/$alias.log" 2>&1 \
        && msg="$($EVAL bench "$OUT/$alias.log")"; then result "$alias" PASS "$msg"
    else result "$alias" FAIL "stream/tool/continuation failed; see $alias.log"; fi
done
cleanup
if msg="$($EVAL mem "$OUT/mem.tsv" --floor "$FLOOR" --min-samples 10)"; then result memory PASS "$msg"
else result memory FAIL "$msg"; fi

# 5 ranks
rank_state > "$OUT/ranks-after.txt"
if msg="$($EVAL ranks "$OUT/ranks-before.txt" "$OUT/ranks-after.txt")"; then result ranks PASS "$msg"
else result ranks FAIL "$msg"; fi

echo "results: $OUT/summary.txt"
if [ "$fails" = 0 ]; then echo "ACCEPTANCE: GO"; exit 0; fi
echo "ACCEPTANCE: NO-GO ($fails failed)"
exit 1
