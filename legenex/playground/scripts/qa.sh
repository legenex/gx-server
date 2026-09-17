#!/usr/bin/env bash
# Central QA gate for GX-Playground:   npm run qa   (or scripts/qa.sh)
# Hermetic: the real Playground proxy in front of the real Control Center
# backend with synthetic cluster data. Uses the Control Center's dev tooling
# (node_modules is a symlink to ../control-ui/node_modules, .venv reused).
#   1 formatting   2 lint   3 unit/proxy tests   4 build validation
#   5 browser E2E + accessibility (axe WCAG 2.2 AA), phone layout   6 security
set -euo pipefail
cd "$(dirname "$0")/.."
ui=../control-ui
step() { printf '\n== %s ==\n' "$*"; }
fail() { echo "QA FAILED: $*" >&2; exit 1; }
[ -e node_modules ] || ln -s ../control-ui/node_modules node_modules
[ -x "${ui}/.venv/bin/ruff" ] || fail "run the Control Center QA once first (it bootstraps .venv and node_modules)"

step "1/6 formatting"
bad=$(grep -rnIE '[[:blank:]]+$' gx_playground tests e2e web scripts systemd 2>/dev/null | head -20 || true)
[ -z "${bad}" ] || fail "trailing whitespace:\n${bad}"
for f in $(git ls-files gx_playground tests e2e web scripts systemd 2>/dev/null); do
  [ -s "$f" ] && [ -n "$(tail -c1 "$f")" ] && fail "missing final newline: $f"
done
echo ok

step "2/6 lint"
"${ui}/.venv/bin/ruff" check --config "${ui}/pyproject.toml" gx_playground tests
bash -n scripts/install.sh scripts/qa.sh
python3 -m compileall -q gx_playground
echo ok

step "3/6 unit and proxy tests"
python3 -m unittest discover -s tests 2>&1 | tail -3

step "4/6 build validation"
node scripts/build-check.mjs

step "5/6 browser E2E + accessibility (offline fixture)"
npx playwright test --project=offline --reporter=line

step "6/6 security"
if command -v gitleaks >/dev/null 2>&1; then
  gitleaks dir . --no-banner --redact --exit-code 1 --log-level warn 2>&1 | tail -5 || fail "gitleaks found a secret"
  echo "gitleaks: no secrets in legenex/playground"
else
  fail "gitleaks not installed"
fi
grep -q 'GX_PG_HOSTS=127.0.0.1,tailscale' systemd/gx-playground.service || fail "unit must bind loopback+tailscale only"
grep -qE '0\.0\.0\.0' systemd/gx-playground.service && fail "wildcard bind in unit"
if grep -rnE 'sk-[A-Za-z0-9]{20,}' web gx_playground >/dev/null; then fail "credential-shaped literal found"; fi
echo ok

printf '\nQA PASSED\n'
