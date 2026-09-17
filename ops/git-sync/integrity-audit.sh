#!/usr/bin/env bash
# ============================================================================
# integrity-audit.sh — daily full integrity audit of the gx-cluster checkout.
#
# On the writer (gx10-01) it verifies the whole triangle:
#     gx10-01 HEAD == origin/main == gx10-02 HEAD
# plus working-tree integrity of critical files on BOTH nodes. On the mirror
# (gx10-02) it verifies its own leg (HEAD == origin/main, clean tree) and
# the same local checks, so each node audits itself even if the other is down.
#
# Checks: GitHub reachable; HEAD equality; clean tree (mirror) / no stuck
# unpushed commits (writer); critical-file content == committed blob, and
# identical on both nodes (writer); no secrets tracked; no model weights or
# large binaries tracked; deployed copies outside the checkout match the repo
# (informational).
#
# Output: ${GX_SYNC_LOG_DIR}/audit-<date>.log and audit-latest.log.
# Exit: 0 all PASS | 1 any FAIL. WARN never fails the audit.
# ============================================================================
set -uo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GX_SYNC_TAG="integrity-audit"
# shellcheck source=./common.sh
source "${here}/common.sh"

ROLE="$(gxs_role)"
OUT="${GX_SYNC_LOG_DIR}/audit-$(date +%Y%m%d-%H%M%S).log"
PASS=0; FAIL=0; WARN=0
r() { # r LEVEL message
  case "$1" in PASS) PASS=$((PASS+1)) ;; FAIL) FAIL=$((FAIL+1)) ;; WARN) WARN=$((WARN+1)) ;; esac
  printf '[%s] %s\n' "$1" "$2" | tee -a "${OUT}"
}

CRITICAL=(
  legenex/lifecycle/gx-max.conf
  legenex/lifecycle/lib.sh
  legenex/lifecycle/gx-max-start.sh
  legenex/lifecycle/gx-max-stop.sh
  legenex/lifecycle/gx-max-unwind.sh
  legenex/lifecycle/gx-max-safety.sh
  legenex/lifecycle/rank1-deadman.sh
  legenex/lifecycle/resource-guard.sh
  legenex/lifecycle/restore-normal.sh
  legenex/lifecycle/node2-holds.sh
  legenex/orchestrator/gx_orchestrator/resource_guard.py
  legenex/gateway/litellm/config.yaml
  legenex/gateway/.env.sample
  legenex/gateway/docker-compose.gateway.yml
  legenex/gateway/docker-compose.node02.yml
  legenex/gateway/llama-swap/node01.yaml
  legenex/gateway/llama-swap/node02.yaml
  legenex/media/docker-compose.media.yml
  legenex/media/router/gx_media_router/server.py
  legenex/media/router/gx_media_router/service.py
  legenex/media/router/gx_media_router/policy.py
  legenex/music/systemd/gx-music.service
  legenex/music/gx_music/engine.py
  legenex/music/gx_music/service.py
  legenex/music/gx_music/config.py
  legenex/music/gx_music/server.py
  legenex/scripts/recover-node2.sh
  legenex/host/kernel-lock/verify-kernel-lock.sh
  ops/git-sync/common.sh
  ops/git-sync/node1-autosync.sh
  ops/git-sync/node2-reconcile.sh
)

{
  echo "==== gx-cluster integrity audit: $(hostname) role=${ROLE} $(date -Is) ===="
  echo "repo: ${GX_SYNC_REPO}"
} | tee "${OUT}"

# ------------------------------------------------------------ remote/HEADs --
remote_sha="$(GIT_TERMINAL_PROMPT=0 timeout "${GX_SYNC_NET_TIMEOUT}" git ls-remote "${GX_SYNC_PUBLIC_URL}" "refs/heads/${GX_SYNC_BRANCH}" 2>/dev/null | awk '{print $1}')"
if [ -n "${remote_sha}" ]; then r PASS "GitHub reachable; origin/${GX_SYNC_BRANCH} = ${remote_sha}"
else r FAIL "GitHub NOT reachable (ls-remote ${GX_SYNC_PUBLIC_URL})"; fi

local_sha="$(g rev-parse HEAD)"
branch="$(g symbolic-ref --quiet --short HEAD 2>/dev/null || echo DETACHED)"
[ "${branch}" = "${GX_SYNC_BRANCH}" ] && r PASS "on branch ${branch}" || r FAIL "on '${branch}', expected ${GX_SYNC_BRANCH}"
if [ -n "${remote_sha}" ]; then
  [ "${local_sha}" = "${remote_sha}" ] && r PASS "$(hostname) HEAD == origin/${GX_SYNC_BRANCH} (${local_sha:0:12})" \
    || r FAIL "$(hostname) HEAD ${local_sha:0:12} != origin/${GX_SYNC_BRANCH} ${remote_sha:0:12}"
fi

dirty="$(g status --porcelain=v1 --untracked-files=all)"
if [ "${ROLE}" = mirror ]; then
  [ -z "${dirty}" ] && r PASS "mirror working tree clean" || r FAIL "mirror working tree DIRTY: $(printf '%s' "${dirty}" | awk '{print $2}' | tr '\n' ' ')"
  [ "$(g remote get-url --push origin 2>/dev/null)" = "DISABLED-gx10-02-is-pull-only" ] \
    && r PASS "mirror push URL disabled" || r FAIL "mirror push URL is NOT disabled"
  g log --format='%s' -20 | grep -q '^autosync(gx10-02)' && r FAIL "mirror history contains gx10-02 autosync commits" \
    || r PASS "no commits authored by the mirror"
else
  [ -z "${dirty}" ] && r PASS "writer working tree clean" \
    || r WARN "writer has uncommitted changes (autosync pending?): $(printf '%s' "${dirty}" | wc -l) path(s)"
  ahead="$(g rev-list --count "origin/${GX_SYNC_BRANCH}..HEAD" 2>/dev/null || echo '?')"
  [ "${ahead}" = 0 ] && r PASS "no unpushed commits" || r FAIL "${ahead} unpushed commit(s) on the writer"
fi

# ----------------------------------------------------- critical file hashes --
manifest="$(mktemp)"
for f in "${CRITICAL[@]}"; do
  blob="$(g rev-parse "HEAD:${f}" 2>/dev/null || true)"
  if [ -z "${blob}" ]; then r FAIL "critical file not tracked: ${f}"; continue; fi
  wt="$(g hash-object "${GX_SYNC_REPO}/${f}" 2>/dev/null || echo missing)"
  [ "${wt}" = "${blob}" ] && echo "${blob}  ${f}" >> "${manifest}" \
    || r FAIL "critical file differs from committed blob: ${f}"
done
r PASS "critical files checked: $(wc -l < "${manifest}")/${#CRITICAL[@]} match HEAD"

if [ "${ROLE}" = writer ]; then
  n2_head="$(timeout 30 ssh -o BatchMode=yes -o ConnectTimeout=10 "${GX_SYNC_NODE2_SSH}" \
    "git -C ~/Documents/Projects/Server/gx-cluster rev-parse HEAD" 2>/dev/null)"
  if [ -z "${n2_head}" ]; then r FAIL "could not read gx10-02 HEAD"
  else
    [ "${n2_head}" = "${local_sha}" ] && r PASS "gx10-01 HEAD == gx10-02 HEAD (${n2_head:0:12})" \
      || r FAIL "gx10-01 HEAD ${local_sha:0:12} != gx10-02 HEAD ${n2_head:0:12}"
    [ -n "${remote_sha}" ] && [ "${n2_head}" = "${remote_sha}" ] && r PASS "gx10-02 HEAD == origin/${GX_SYNC_BRANCH}"
    # shellcheck disable=SC2029
    n2_hashes="$(timeout 30 ssh -o BatchMode=yes "${GX_SYNC_NODE2_SSH}" \
      "cd ~/Documents/Projects/Server/gx-cluster && for f in ${CRITICAL[*]}; do printf '%s  %s\n' \"\$(git hash-object \"\$f\" 2>/dev/null || echo missing)\" \"\$f\"; done" 2>/dev/null)"
    if diff <(sort -k2 "${manifest}") <(printf '%s\n' "${n2_hashes}" | sort -k2) >/dev/null; then
      r PASS "critical files byte-identical on gx10-01 and gx10-02"
    else
      r FAIL "critical files differ between nodes: $(diff <(sort -k2 "${manifest}") <(printf '%s\n' "${n2_hashes}" | sort -k2) | awk '/^[<>]/{print $3}' | sort -u | tr '\n' ' ')"
    fi
  fi
fi
rm -f "${manifest}"

# ------------------------------------------------ tracked-content hygiene --
bad_paths="$(g ls-files | while IFS= read -r p; do gxs_forbidden_path "${p}" && echo "${p}"; done)"
[ -z "${bad_paths}" ] && r PASS "no forbidden paths tracked (secrets/weights/archives/state)" \
  || r FAIL "forbidden paths tracked: $(printf '%s' "${bad_paths}" | tr '\n' ' ')"

large="$(g ls-tree -r -l HEAD | awk -v max="${GX_SYNC_MAX_FILE_BYTES}" '$4 != "-" && $4+0 > max {print $5}' \
  | while IFS= read -r p; do case " ${GX_SYNC_LARGE_ALLOW} " in *" ${p} "*) ;; *) echo "${p}" ;; esac; done)"
[ -z "${large}" ] && r PASS "no unexpected large files tracked (> $((GX_SYNC_MAX_FILE_BYTES/1048576)) MiB; allow-listed: ${GX_SYNC_LARGE_ALLOW})" \
  || r FAIL "large files tracked: $(printf '%s' "${large}" | tr '\n' ' ')"

bin_models="$(g ls-files | grep -E '\.(gguf|safetensors|ckpt|pt|pth|onnx)$' || true)"
[ -z "${bin_models}" ] && r PASS "no model weight files tracked" || r FAIL "model weight files tracked: ${bin_models}"

if [ -x "${GX_SYNC_GITLEAKS}" ]; then
  tree="$(mktemp -d)"
  g archive HEAD | tar -x -C "${tree}"
  if "${GX_SYNC_GITLEAKS}" dir "${tree}" --redact --no-banner --log-level error --exit-code 1 >/dev/null 2>&1; then
    r PASS "gitleaks: no secrets in the tracked tree at HEAD"
  else
    r FAIL "gitleaks: possible secrets in the tracked tree at HEAD (run gitleaks dir on a git archive to inspect; values are not logged here)"
  fi
  rm -rf "${tree}"
else
  # `git grep ... HEAD` prints HEAD:<file>:<line>:<text>; obvious documentation
  # placeholders (ghp_your..., hf_example...) are not credentials.
  pat='(ghp_|gho_|github_pat_)[A-Za-z0-9_]{20,}|tskey-[A-Za-z0-9-]{10,}|hf_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----'
  hits="$(g grep -I -n -E "${pat}" HEAD 2>/dev/null \
    | grep -viE '(ghp_|gho_|github_pat_|hf_)(your|example|xxxx|placeholder|changeme)' \
    | cut -d: -f2 | sort -u || true)"
  [ -z "${hits}" ] && r PASS "regex scan: no credential patterns in the tracked tree (gitleaks not installed on this node)" \
    || r FAIL "regex scan: credential-like patterns in: $(printf '%s' "${hits}" | tr '\n' ' ')"
fi

# ------------------------------------ deployed copies (informational only) --
check_copy() { # check_copy <deployed path> <repo path>
  [ -f "$1" ] || return 0
  if cmp -s "$1" "${GX_SYNC_REPO}/$2"; then r PASS "deployed copy matches repo: $1"
  else r WARN "deployed copy differs from repo: $1 (vs $2)"; fi
}
if [ "${ROLE}" = mirror ]; then
  check_copy "${HOME}/.gx-guard/rank1-deadman.sh" legenex/lifecycle/rank1-deadman.sh
  check_copy "${HOME}/.gx-guard/gx-max-safety.sh" legenex/lifecycle/gx-max-safety.sh
  check_copy "${HOME}/gx-gateway/docker-compose.node02.yml" legenex/gateway/docker-compose.node02.yml
  check_copy "${HOME}/gx-gateway/node02.yaml" legenex/gateway/llama-swap/node02.yaml
  check_copy "${HOME}/gx-gateway/env/gx-reason.env" legenex/gateway/llama-swap/env/gx-reason.env
  check_copy "${HOME}/gx-media/docker-compose.media.yml" legenex/media/docker-compose.media.yml
  check_copy "${HOME}/gx-media/comfyui/extra_model_paths.yaml" legenex/media/comfyui/extra_model_paths.yaml
  for wf in "${GX_SYNC_REPO}"/legenex/media/workflows/*.json; do
    check_copy "${HOME}/gx-media/workflows/$(basename "${wf}")" "legenex/media/workflows/$(basename "${wf}")"
  done
  check_copy "${HOME}/gx-kernel-lock/verify-kernel-lock.sh" legenex/host/kernel-lock/verify-kernel-lock.sh
  # gx-music (D-036): the user unit is a symlink into this checkout, and the
  # supervisor must run from the checkout, not from the Stage A staging tree.
  munit="${HOME}/.config/systemd/user/gx-music.service"
  if [ -e "${munit}" ]; then
    if [ "$(readlink -f "${munit}")" = "${GX_SYNC_REPO}/legenex/music/systemd/gx-music.service" ]; then
      r PASS "gx-music unit is the checkout's unit (symlink)"
    else
      r WARN "gx-music unit is not a symlink to the checkout: ${munit}"
    fi
    if grep -qs '^GX_MUSIC_HOME=' "${HOME}/.config/gx-music/gx-music.env"; then
      r WARN "gx-music.env overrides GX_MUSIC_HOME (supervisor may run from outside the checkout)"
    fi
    if systemctl --user is-active --quiet gx-music.service; then
      r PASS "gx-music supervisor active"
    else
      r WARN "gx-music supervisor is not active"
    fi
  fi
  # The retired ~/gx-worker tree must never come back as a runtime dependency:
  # the deploy dirs are real directories, not links into it.
  for d in gx-gateway gx-media gx-kernel-lock; do
    [ -L "${HOME}/${d}" ] && r WARN "deploy dir ${HOME}/${d} is a symlink to $(readlink "${HOME}/${d}"); expected a real directory"
  done
  [ -e "${HOME}/gx-worker" ] && r WARN "legacy ${HOME}/gx-worker exists again; it must not be an operational dependency"
else
  check_copy "${HOME}/gx-kernel-lock/verify-kernel-lock.sh" legenex/host/kernel-lock/verify-kernel-lock.sh
  check_copy "${HOME}/.config/systemd/user/gx-orchestrator.service" legenex/orchestrator/systemd/gx-orchestrator.service
  # Installed by legenex/control-ui/scripts/install.sh with @REPO@ rendered.
  unit="${HOME}/.config/systemd/user/gx-control-ui.service"
  if [ -f "${unit}" ]; then
    if sed "s#@REPO@#${GX_SYNC_REPO}#g" "${GX_SYNC_REPO}/legenex/control-ui/systemd/gx-control-ui.service" | cmp -s - "${unit}"; then
      r PASS "deployed unit matches rendered repo template: ${unit}"
    else
      r WARN "deployed unit differs from the rendered repo template: ${unit} (run legenex/control-ui/scripts/install.sh)"
    fi
  fi
  # The running gateway must hold the media key from the ignored .env (a recreate
  # from a shell with a stale variable once left it on the placeholder). Only
  # hashes are compared; no key is printed.
  envf="${GX_SYNC_REPO}/legenex/gateway/.env"
  if [ -r "${envf}" ] && docker inspect gx-litellm >/dev/null 2>&1; then
    want="$(sed -n 's/^GX_MEDIA_API_KEY=//p' "${envf}" | tail -1 | tr -d '\n' | sha256sum | cut -c1-16)"
    have="$(docker exec gx-litellm printenv GX_MEDIA_API_KEY 2>/dev/null | tr -d '\n' | sha256sum | cut -c1-16)"
    if [ "${want}" = "${have}" ]; then
      r PASS "gx-litellm media key matches legenex/gateway/.env"
    else
      r FAIL "gx-litellm media key differs from legenex/gateway/.env (recreate: cd legenex/gateway && docker compose -f docker-compose.gateway.yml up -d --no-deps litellm)"
    fi
  fi
fi

echo "==== result: PASS=${PASS} WARN=${WARN} FAIL=${FAIL} ====" | tee -a "${OUT}"
cp -f "${OUT}" "${GX_SYNC_LOG_DIR}/audit-latest.log"
[ "${FAIL}" -eq 0 ]
