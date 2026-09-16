#!/usr/bin/env bash
# ============================================================================
# gx-max-inference.sh — real-output checks against a RUNNING gx-max engine.
#
# Does not start or stop anything. Run it after gx-max-start.sh reports READY
# (directly against the engine, default) or through the gateway alias
# (GX_MAX_BASE=http://127.0.0.1:4000/v1 GX_MAX_MODEL=gx-max GX_MAX_KEY=...).
#
# Checks: /health, /v1/models, a factual answer, a reasoning answer, a coding
# answer, a several-hundred-token generation (decode tok/s), streaming TTFT,
# and RDMA hardware-counter traffic on the ConnectX rails during generation
# (proof the two ranks are talking over the fabric, not Tailscale).
#
# Exit code = number of failed checks. Results also go to
# ${GX_MAX_RESULTS:-/srv/logs/gx-max-inference-<ts>.json}.
# ============================================================================
set -uo pipefail
BASE="${GX_MAX_BASE:-http://127.0.0.1:30000/v1}"
ENGINE="${GX_MAX_ENGINE:-http://127.0.0.1:30000}"
MODEL="${GX_MAX_MODEL:-/model}"
KEY="${GX_MAX_KEY:-none}"
RESULTS="${GX_MAX_RESULTS:-/srv/logs/gx-max-inference-$(date -u +%Y%m%dT%H%M%SZ).json}"
PASS=0; FAIL=0
pass(){ PASS=$((PASS+1)); printf 'PASS  %s\n' "$1"; }
fail(){ FAIL=$((FAIL+1)); printf 'FAIL  %s :: %s\n' "$1" "$2"; }

rdma_bytes() { # total xmit+rcv bytes across all RoCE ports (counters are in 4-octet units)
  local t=0 f
  for f in /sys/class/infiniband/*/ports/*/counters/port_xmit_data /sys/class/infiniband/*/ports/*/counters/port_rcv_data; do
    [ -r "$f" ] && t=$(( t + $(cat "$f") * 4 ))
  done
  echo "$t"
}

chat() { # chat <max_tokens> <prompt>  -> writes JSON response to stdout
  python3 - "$BASE" "$MODEL" "$KEY" "$1" "$2" <<'PY'
import json, sys, time, urllib.request
base, model, key, max_tokens, prompt = sys.argv[1:6]
body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                   "max_tokens": int(max_tokens), "temperature": 0}).encode()
req = urllib.request.Request(base + "/chat/completions", data=body,
                             headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
t0 = time.time()
with urllib.request.urlopen(req, timeout=900) as r:
    d = json.load(r)
d["_wall_s"] = time.time() - t0
print(json.dumps(d))
PY
}

text_of() { python3 -c 'import json,sys; d=json.load(sys.stdin); m=d["choices"][0]["message"]; print(((m.get("content") or "") + " " + (m.get("reasoning_content") or "")).strip())'; }

declare -A R

code=$(curl -s -o /dev/null -w '%{http_code}' -m 10 "${ENGINE}/health")
[ "$code" = 200 ] && pass "/health 200" || fail "/health" "HTTP $code"
models=$(curl -s -m 10 -H "Authorization: Bearer ${KEY}" "${BASE}/models")
echo "$models" | grep -q '"id"' && pass "/v1/models lists: $(echo "$models" | python3 -c 'import json,sys; print([m["id"] for m in json.load(sys.stdin)["data"]])')" \
  || fail "/v1/models" "$models"

out=$(chat 64 "What is the capital city of Australia? Answer in one word.")
t=$(echo "$out" | text_of)
echo "$t" | grep -qi canberra && pass "factual: $(echo "$t" | head -c 80)" || fail "factual" "$t"
R[factual]="$t"

out=$(chat 1024 "A bat and a ball cost \$1.10 in total. The bat costs \$1.00 more than the ball. How much does the ball cost? Reply with the final amount.")
t=$(echo "$out" | text_of)
echo "$t" | grep -qE '0\.05|5 cents|five cents' && pass "reasoning: bat-and-ball -> \$0.05" || fail "reasoning" "$(echo "$t" | tail -c 200)"
R[reasoning]="$(echo "$t" | tail -c 300)"

out=$(chat 512 "Write a Python function is_prime(n) that returns True if n is prime. Only output the code.")
t=$(echo "$out" | text_of)
if echo "$t" | grep -q 'def is_prime'; then
  code_py=$(printf '%s\n' "$t" | awk '/```/{f=!f; next} f' ); [ -z "$code_py" ] && code_py="$t"
  if printf '%s\n' "$code_py" | python3 -c '
import sys
ns = {}
exec(sys.stdin.read(), ns)
f = ns["is_prime"]
assert [n for n in range(30) if f(n)] == [2,3,5,7,11,13,17,19,23,29]
' 2>/dev/null; then pass "coding: is_prime runs and is correct for 0..29"; else fail "coding" "code did not execute correctly"; fi
else
  fail "coding" "$(echo "$t" | head -c 200)"
fi

rb0=$(rdma_bytes)
out=$(chat 700 "Write a detailed, multi-paragraph explanation of how tensor parallelism splits a transformer layer across two GPUs, including attention and MLP blocks.")
rb1=$(rdma_bytes)
read -r ctoks wall < <(echo "$out" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["usage"]["completion_tokens"], round(d["_wall_s"],2))')
tps=$(python3 -c "print(round($ctoks/$wall,2))")
[ "${ctoks:-0}" -ge 300 ] && pass "long generation: ${ctoks} tokens in ${wall}s = ${tps} tok/s (wall, incl. prefill)" \
  || fail "long generation" "only ${ctoks} tokens"
rdma_mb=$(( (rb1 - rb0) / 1048576 ))
[ "$rdma_mb" -gt 10 ] && pass "RDMA traffic during generation: ${rdma_mb} MiB across ConnectX ports" \
  || fail "RDMA traffic" "only ${rdma_mb} MiB -- ranks may not be using the fabric"

ttft=$(python3 - "$BASE" "$MODEL" "$KEY" <<'PY'
import json, sys, time, urllib.request
base, model, key = sys.argv[1:4]
body = json.dumps({"model": model, "stream": True, "max_tokens": 32, "temperature": 0,
                   "messages": [{"role": "user", "content": "Say hello."}]}).encode()
req = urllib.request.Request(base + "/chat/completions", data=body,
                             headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
t0 = time.time()
with urllib.request.urlopen(req, timeout=300) as r:
    for line in r:
        if not line.startswith(b"data:") or b"[DONE]" in line:
            continue
        try:
            delta = json.loads(line[5:])["choices"][0].get("delta", {})
        except (ValueError, KeyError, IndexError):
            continue
        if delta.get("content") or delta.get("reasoning_content"):
            print(round(time.time() - t0, 3)); break
PY
)
[ -n "$ttft" ] && pass "streaming TTFT ${ttft}s" || fail "TTFT" "no streamed token"

python3 - "$RESULTS" <<PY
import json, sys
json.dump({"pass": $PASS, "fail": $FAIL, "long_tokens": "${ctoks:-}", "long_wall_s": "${wall:-}",
           "long_tok_s": "${tps:-}", "rdma_mib": ${rdma_mb:-0}, "ttft_s": "${ttft:-}"}, open(sys.argv[1], "w"), indent=2)
PY
printf '\n PASS=%d FAIL=%d  (results: %s)\n' "$PASS" "$FAIL" "$RESULTS"
exit "$FAIL"
