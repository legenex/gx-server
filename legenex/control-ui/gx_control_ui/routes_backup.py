"""Backup & Recovery API for GX-Cluster Control UI."""
from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path

from .server import Handler, route

ROOT = Path(os.environ.get("GX_BACKUP_ROOT", str(Path.home() / "Documents/Projects/gx-backup")))
LATEST = Path.home() / "Documents/Backups/LATEST"
STATE = Path.home() / "Documents/Backups/last-backup.json"
_lock = threading.Lock()
_job: dict = {"running": False, "log": ""}


def _restic(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PATH"] = str(Path.home() / ".local/bin") + ":" + env.get("PATH", "")
    env["RESTIC_PASSWORD_FILE"] = str(Path.home() / ".config/gx-backup/restic-password")
    repo = str(Path.home() / "Documents/Backups/restic/local")
    return subprocess.run(
        ["restic", "-r", repo, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


@route("GET", r"/api/backup/status")
def api_backup_status(h: Handler) -> None:
    ident = LATEST.read_text().strip() if LATEST.exists() else None
    snaps = ""
    try:
        r = _restic(["snapshots", "--json"], timeout=20)
        snaps = r.stdout if r.returncode == 0 else r.stderr
    except Exception as exc:  # noqa: BLE001
        snaps = str(exc)
    parsed = []
    try:
        parsed = json.loads(snaps) if snaps.startswith("[") else []
    except json.JSONDecodeError:
        parsed = []
    h._json(200, {
        "latest": ident,
        "running": _job["running"],
        "repo": str(Path.home() / "Documents/Backups/restic/local"),
        "peer": str(Path.home() / "Documents/Backups/restic/peer-gx10-02"),
        "github": "https://github.com/legenex/gx-backup",
        "offsite": "absent — cross-node encrypted restic + private Git recipe only",
        "snapshots": parsed[-8:],
        "guide_md": "/docs/DISASTER-RECOVERY.md",
        "guide_pdf": str(ROOT / "docs/GX-BACKUP-RESTORE-GUIDE.pdf"),
    })


@route("POST", r"/api/backup/now")
def api_backup_now(h: Handler) -> None:
    body = h._body(4096)
    if _job["running"]:
        h._json(409, {"ok": False, "error": "backup already running"})
        return

    def worker() -> None:
        _job["running"] = True
        try:
            env = os.environ.copy()
            env["PATH"] = str(Path.home() / ".local/bin") + ":" + env.get("PATH", "")
            r = subprocess.run(
                ["bash", str(ROOT / "backup/restic.sh"), "backup"],
                capture_output=True,
                text=True,
                timeout=7200,
                env=env,
            )
            _job["log"] = (r.stdout or "")[-4000:] + (r.stderr or "")[-1000:]
            _job["rc"] = r.returncode
        except Exception as exc:  # noqa: BLE001
            _job["log"] = str(exc)
            _job["rc"] = 1
        finally:
            _job["running"] = False

    threading.Thread(target=worker, daemon=True, name="gx-backup-now").start()
    h._json(202, {"ok": True, "started": True})


@route("POST", r"/api/backup/verify")
def api_backup_verify(h: Handler) -> None:
    r = _restic(["check", "--read-data-subset=2%"], timeout=600)
    h._json(200 if r.returncode == 0 else 500, {"ok": r.returncode == 0, "output": (r.stdout or r.stderr)[-4000:]})
