#!/usr/bin/env bash
# seed_repo.sh — create the disposable coding-bench repo.
#
# Usage: seed_repo.sh [target_dir]
#   default target: /tmp/gx-coding-bench-<unix-ts>
#
# The repo is a deliberately rough stdlib-only task-tracker REST app with
# known gaps the multi-agent workflow must find and fix:
#   * no input validation (add_task accepts anything)
#   * no error handling (get_task raises a raw KeyError)
#   * missing tests (no tests/ directory at all)
#   * ONE seeded bug: delete_task removes the LAST task regardless of id
# Deterministic: same files every time, no network, no external deps.
# Idempotent: re-running against an existing dir rewrites identical content
# and only re-commits when something actually changed.

set -euo pipefail

TARGET="${1:-/tmp/gx-coding-bench-$(date +%s)}"
mkdir -p "$TARGET"

write_file() {
    local path="$1"
    mkdir -p "$(dirname "$path")"
    cat > "$path"
}

# ---------------------------------------------------------------- app/tracker.py
write_file "$TARGET/tracker.py" <<'EOF'
"""Task tracker core (deliberately rough -- this is a coding-bench target).

Known rough edges (do not "fix" while seeding; the workflow under test is
supposed to find and fix them):
  * add_task does no validation
  * get_task raises a raw KeyError
  * delete_task removes the LAST task whatever id you pass (seeded bug)
  * there are no tests
"""

from __future__ import annotations

import time

tasks: dict[int, dict] = {}
_next_id = 1


def add_task(title, due=None):
    """Add a task. No validation of any kind (gap)."""
    global _next_id
    task = {
        "id": _next_id,
        "title": title,
        "due": due,
        "done": False,
        "created_at": time.time(),
    }
    tasks[_next_id] = task
    _next_id += 1
    return task


def get_task(task_id):
    """Return a task by id. Raises a raw KeyError (gap)."""
    return tasks[task_id]


def list_tasks(done=None):
    """List tasks, optionally filtered by done status."""
    result = list(tasks.values())
    if done is not None:
        result = [t for t in result if t["done"] is done]
    return result


def complete_task(task_id):
    """Mark a task done."""
    tasks[task_id]["done"] = True
    return tasks[task_id]


def delete_task(task_id):
    """Delete a task by id.

    SEEDED BUG: ignores task_id and removes the most recently added task.
    """
    if not tasks:
        raise KeyError(task_id)
    return tasks.popitem()[1]


def reset():
    """Clear all tasks (used by tests and the smoke check)."""
    global _next_id
    tasks.clear()
    _next_id = 1


def main():
    reset()
    first = add_task("first")
    second = add_task("second")
    delete_task(first["id"])
    remaining = list_tasks()
    print(f"remaining after deleting task {first['id']}: {[t['id'] for t in remaining]}")


if __name__ == "__main__":
    main()
EOF

# ---------------------------------------------------------------- app/server.py
write_file "$TARGET/server.py" <<'EOF'
"""Minimal REST wrapper around tracker.py (stdlib http.server only).

Endpoints:
  GET  /tasks            -> list
  GET  /tasks/<id>       -> one (raw KeyError -> 500, gap)
  POST /tasks {"title": ..., "due": ...} -> created
  POST /tasks/<id>/complete
  DELETE /tasks/<id>
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer

import tracker


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.rstrip("/")
        if path == "/tasks":
            return self._send(200, tracker.list_tasks())
        if path.startswith("/tasks/"):
            try:
                task = tracker.get_task(int(path.rsplit("/", 1)[1]))
            except KeyError:
                return self._send(500, {"error": "internal error"})  # gap
            return self._send(200, task)
        self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.rstrip("/")
        if path == "/tasks":
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
            return self._send(201, tracker.add_task(data.get("title"), data.get("due")))
        if path.endswith("/complete"):
            task_id = int(path.split("/")[2])
            return self._send(200, tracker.complete_task(task_id))
        self._send(404, {"error": "not found"})

    def do_DELETE(self):
        path = self.path.rstrip("/")
        if path.startswith("/tasks/"):
            return self._send(200, tracker.delete_task(int(path.rsplit("/", 1)[1])))
        self._send(404, {"error": "not found"})

    def log_message(self, *args):
        pass


def serve(port=8080):
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    serve()
EOF

# ---------------------------------------------------------------- README
write_file "$TARGET/README.md" <<'EOF'
# task-tracker (coding-bench target repo)

A deliberately rough stdlib task-tracker REST app. The mission of the
multi-agent workflow that works on this repo:

1. inspect the repo and understand the tracker core,
2. plan fixes for the known gaps: input validation, error handling,
   the missing test suite, and one seeded bug in delete_task,
3. implement them without breaking the API,
4. write tests (stdlib unittest, pytest-compatible) under tests/,
5. review and repair until the final validator passes.

The success bar: tests green, reviewer verdict PASS, bounded wall time.
EOF

# ---------------------------------------------------------------- git snapshot
cd "$TARGET"
if command -v git >/dev/null 2>&1; then
    git init -q 2>/dev/null || true
    git add -A 2>/dev/null || true
    if ! git diff --cached --quiet 2>/dev/null; then
        git -c user.name="gx-coding-bench" -c user.email="bench@gx.local" \
            commit -q -m "seed: task-tracker target repo" 2>/dev/null || true
    fi
fi

echo "$TARGET"
