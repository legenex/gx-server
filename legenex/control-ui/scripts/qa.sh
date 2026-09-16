#!/usr/bin/env bash
# Central QA gate for the control UI:   npm run qa   (or scripts/qa.sh)
#
# Hermetic: no cluster access, no real credentials, temp dirs only.
# Tooling is project-local: .venv (ruff, mypy) and node_modules (Playwright,
# axe-core) are created on first run and are ignored by Git.
#
#   1  formatting (whitespace, final newline, no tabs in Python)
#   2  lint (ruff; bash -n; frontend lint inside the build check)
#   3  type check (mypy)
#   4  unit + component + API/integration + auth tests (unittest)
#   5  performance budget (API p95, compression, asset size)
#   6  build validation (ES modules, imports, CSP safety, size budget)
#   7  browser E2E incl. accessibility (axe WCAG 2.2 AA), mobile layout
#   8  security: secret scan (gitleaks), dependency audit (npm), config checks
set -euo pipefail
cd "$(dirname "$0")/.."
ui="$(pwd)"
step() { printf '\n== %s ==\n' "$*"; }
fail() { echo "QA FAILED: $*" >&2; exit 1; }

if [ ! -x .venv/bin/ruff ] || [ ! -x .venv/bin/mypy ]; then
  step "bootstrap .venv (ruff, mypy)"
  /usr/bin/python3 -m venv .venv
  .venv/bin/pip install -q ruff mypy
fi
if [ ! -d node_modules/@playwright/test ]; then
  step "bootstrap node_modules (npm ci)"
  npm ci --no-fund --no-audit
  npx playwright install chromium
fi

step "1/8 formatting"
bad=$(grep -rnIE '[[:blank:]]+$' gx_control_ui tests e2e web scripts docs systemd --include='*' \
  --exclude-dir=node_modules 2>/dev/null | head -20 || true)
[ -z "${bad}" ] || fail "trailing whitespace:\n${bad}"
tabs=$(grep -rnP '\t' gx_control_ui tests e2e --include='*.py' | head -5 || true)
[ -z "${tabs}" ] || fail "tabs in Python:\n${tabs}"
for f in $(git ls-files gx_control_ui tests e2e web scripts docs systemd 2>/dev/null); do
  [ -s "$f" ] && [ -n "$(tail -c1 "$f")" ] && fail "missing final newline: $f"
done
echo "ok"

step "2/8 lint"
.venv/bin/ruff check gx_control_ui tests e2e
for s in scripts/*.sh scripts/gx-ui-passwd; do bash -n "$s"; done
python3 -m compileall -q gx_control_ui
echo "ok"

step "3/8 type check"
.venv/bin/mypy gx_control_ui

step "4/8 unit, component, API and auth tests"
python3 -m unittest discover -s tests -p 'test_*.py' 2>&1 | tail -4

step "5/8 performance budget"
python3 -m unittest discover -s tests -p 'test_perf.py' 2>&1 | tail -2

step "6/8 build validation"
node scripts/build-check.mjs

step "7/8 browser E2E + accessibility (offline fixture)"
npx playwright test --project=offline --reporter=line

step "8/8 security"
if command -v gitleaks >/dev/null 2>&1; then
  gitleaks dir "${ui}" --no-banner --redact --exit-code 1 \
    --log-level warn 2>&1 | tail -5 || fail "gitleaks found a secret"
  echo "gitleaks: no secrets in legenex/control-ui"
else
  fail "gitleaks not installed (required; the Git autosync gate uses it too)"
fi
npm audit --omit=optional --audit-level=moderate
grep -q 'GX_UI_HOSTS=127.0.0.1,tailscale' systemd/gx-control-ui.service || fail "unit must bind loopback+tailscale only"
grep -qE '0\.0\.0\.0' systemd/gx-control-ui.service && fail "wildcard bind in unit"
if grep -rnE 'sk-[A-Za-z0-9]{20,}' web docs gx_control_ui >/dev/null; then fail "credential-shaped literal found"; fi
secret_dir=/srv/projects/gx-cluster/secrets/control-ui
if [ -d "${secret_dir}" ]; then
  [ "$(stat -c %a "${secret_dir}")" = 700 ] || fail "${secret_dir} must be 0700"
  for f in "${secret_dir}"/*; do
    [ -e "$f" ] || continue
    [ "$(stat -c %a "$f")" = 600 ] || fail "$f must be 0600"
  done
fi
echo "ok"

printf '\nQA PASSED\n'
