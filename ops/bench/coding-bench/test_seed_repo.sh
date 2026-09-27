#!/usr/bin/env bash
# test_seed_repo.sh — hermetic idempotence check for the coding-bench seeder.
#
# Verifies: seeding twice into the same dir produces byte-identical content,
# the seeded repo compiles, and the seeded bug is present (deterministic).

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d /tmp/gx-seed-test-XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

TARGET="$WORK/repo"

"$HERE/seed_repo.sh" "$TARGET" >/dev/null

# snapshot of tracked file contents after the first seed
SUMS1="$(cd "$TARGET" && find . -type f -not -path './.git/*' | sort | xargs sha256sum | sha256sum)"

# second seed into the SAME directory (the idempotence case)
"$HERE/seed_repo.sh" "$TARGET" >/dev/null

SUMS2="$(cd "$TARGET" && find . -type f -not -path './.git/*' | sort | xargs sha256sum | sha256sum)"

if [ "$SUMS1" != "$SUMS2" ]; then
    echo "FAIL: seed_repo.sh is not idempotent (content changed on re-seed)"
    exit 1
fi
echo "PASS: re-seed is byte-identical"

# the seeded files must be valid Python
python3 -m py_compile "$TARGET/tracker.py" "$TARGET/server.py"
echo "PASS: seeded files compile"

# the seeded bug must be there: delete_task removes the LAST task whatever id
python3 - "$TARGET" <<'EOF'
import sys
sys.path.insert(0, sys.argv[1])
import tracker

tracker.reset()
first = tracker.add_task("first")
second = tracker.add_task("second")
deleted = tracker.delete_task(first["id"])
if deleted["id"] != second["id"]:
    print("FAIL: seeded bug disappeared (delete_task deleted the right task)")
    sys.exit(1)
print("PASS: seeded bug present (delete_task removes the last task)")
EOF

# idempotent git state: a re-seed must not create a dirty tree
if (cd "$TARGET" && git diff --quiet && git diff --cached --quiet) 2>/dev/null; then
    echo "PASS: git tree clean after re-seed"
else
    echo "FAIL: re-seed left the git tree dirty"
    exit 1
fi

echo "ALL SEED TESTS PASSED"
