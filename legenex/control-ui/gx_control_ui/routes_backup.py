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
    check_path = Path.home() / "Documents/Backups/last-restic-check.json"
    drill_path = Path.home() / "Documents/Backups/last-restore-drill.json"
    check = {}
    drill = {}
    try:
        check = json.loads(check_path.read_text()) if check_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        check = {}
    try:
        drill = json.loads(drill_path.read_text()) if drill_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        drill = {}
    latest_time = None
    if parsed:
        latest_time = parsed[-1].get("time")
    h._json(200, {
        "latest": ident,
        "latest_snapshot": (parsed[-1] if parsed else None),
        "snapshot_count": len(parsed),
        "running": _job["running"],
        "repo": str(Path.home() / "Documents/Backups/restic/local"),
        "peer": str(Path.home() / "Documents/Backups/restic/peer-gx10-02"),
        "github": "https://github.com/legenex/gx-backup",
        "offsite": "Not configured",
        "offsite_note": "Cross-node encrypted restic is not geographic/offsite disaster recovery. Losing both GX10s loses mutable data.",
        "snapshots": parsed[-8:],
        "integrity": check,
        "restore_drill": drill,
        "recovery_tested": bool(drill.get("ok")),
        "coverage": (
            "GX Cluster source, four-mode gateway/LiteLLM, Postgres dumps, OpenWebUI volume, "
            "AgentOS, systemd user units, secrets, scripts, docs, model manifests. "
            "Public weights are recreated from models.lock. GX-Playground/media are in history, "
            "not in the normal production recovery recipe."
        ),
        "guide_md": "/docs/DISASTER-RECOVERY.md",
        "guide_pdf": str(ROOT / "docs/GX-BACKUP-RESTORE-GUIDE.pdf"),
        "latest_time": latest_time,
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
    ok = r.returncode == 0
    record = {
        "ok": ok,
        "at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "subset": "2%",
    }
    try:
        (Path.home() / "Documents/Backups/last-restic-check.json").write_text(json.dumps(record) + "\n")
    except OSError:
        pass
    h._json(200 if ok else 500, {"ok": ok, "output": (r.stdout or r.stderr)[-4000:], **record})
