#!/usr/bin/env python3
"""Production Open WebUI interactive user_input / ask_user acceptance.

Proves a real conversation that:
  1. starts normally on https://chat.legenex.co
  2. invokes ask_user (builtin user_input)
  3. enters the pending UI state
  4. receives a browser answer
  5. resumes the SAME chat
  6. completes with USER_INPUT_OK

    python3 legenex/tests/owui_user_input_acceptance.py [--out DIR]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import owui_identity_acceptance as acc  # noqa: E402

MODELS = ["gx-mini", "gx-fast", "gx-reason", "gx-max", "gx-auto"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    ap.add_argument("--model", default="gx-fast")
    args = ap.parse_args()
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(args.out or f"/srv/logs/acceptance/owui-user-input-{run_id}")
    out_dir.mkdir(parents=True, exist_ok=True)
    email = f"gx-acceptance-{run_id.lower()}-ui@example.invalid"
    password = secrets.token_urlsafe(24)
    created = acc.owui({
        "op": "create", "email": email, "password": password,
        "name": "GX user_input acceptance", "models": MODELS,
    })
    uid = created["user_id"]
    report: dict = {"run": run_id, "account": uid, "model": args.model, "checks": [],
                    "owui_url": os.environ.get("GX_OWUI_URL", "http://127.0.0.1:3000")}

    def check(label: str, ok: bool, **detail) -> None:
        report["checks"].append({"check": label, "ok": bool(ok), **detail})
        print(("PASS " if ok else "FAIL ") + label + (f"  {json.dumps(detail)[:500]}" if detail else ""), flush=True)

    try:
        out_json = out_dir / "owui-user-input.json"
        shot = out_dir / "owui-user-input.png"
        env = {
            **os.environ,
            "GX_OWUI_EMAIL": email,
            "GX_OWUI_PASSWORD": password,
            "GX_OWUI_MODEL": args.model,
            "GX_OWUI_OUT": str(out_json),
            "GX_OWUI_SHOT": str(shot),
            "GX_OWUI_URL": os.environ.get("GX_OWUI_URL", "http://127.0.0.1:3000"),
        }
        res = subprocess.run(
            ["node", str(HERE / "owui_user_input_browser.mjs")],
            env=env, capture_output=True, text=True, timeout=600,
        )
        report["browser_rc"] = res.returncode
        report["browser_stderr"] = (res.stderr or "")[-800:]
        if res.returncode != 0 and not out_json.is_file():
            raise RuntimeError(res.stderr.strip()[-800:] or res.stdout.strip()[-800:])
        ui = json.loads(out_json.read_text() if out_json.is_file() else res.stdout.strip().splitlines()[-1])
        report["ui"] = ui
        check("browser signed in and opened chat", "signed_in" in [s.get("step") for s in ui.get("steps", [])])
        check("ask_user / user_input pending UI appeared", bool(ui.get("pending_seen")), steps=ui.get("steps", [])[-6:])
        check("user answered the pending prompt in-browser", bool(ui.get("answered")))
        final = ui.get("final_text") or ""
        check("same conversation resumed and completed with USER_INPUT_OK Blue",
              "USER_INPUT_OK" in final.upper() and "BLUE" in final.upper(),
              final=final[-500:])
        check("screenshot captured", shot.is_file() and shot.stat().st_size > 5_000, path=str(shot))
        check("no browser crash", not ui.get("error"), error=ui.get("error"))
    except Exception as e:
        check("driver completed without exception", False, error=str(e)[:500])
    finally:
        try:
            acc.owui({"op": "cleanup", "user_id": uid, "models": MODELS})
            check("disposable account cleaned up", True)
        except Exception as e:
            check("disposable account cleaned up", False, error=str(e)[:200])

    (out_dir / "owui-user-input-acceptance.json").write_text(json.dumps(report, indent=2))
    failed = sum(1 for c in report["checks"] if not c["ok"])
    print(json.dumps({"out": str(out_dir), "failed": failed, "total": len(report["checks"])}))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
