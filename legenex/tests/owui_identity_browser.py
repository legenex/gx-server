#!/usr/bin/env python3
"""Drive owui_identity_browser.mjs against production Open WebUI (D-038).

Creates a disposable `user` account (read access to the gx-* entries only),
signs in through https://chat.legenex.co in system Chrome, asks gx-mini who it
is in a NEW chat, checks that Open WebUI stored that chat with model gx-mini
and the identity answer, then deletes the account (and its chat).

    python3 legenex/tests/owui_identity_browser.py [--out DIR]
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
_CHATS = r'''
import sqlite3, json, sys
uid = sys.argv[1]
c = sqlite3.connect("file:/app/backend/data/webui.db?mode=ro", uri=True)
out = []
for cid, title, chat in c.execute("select id, title, chat from chat where user_id = ?", (uid,)):
    d = json.loads(chat or "{}")
    msgs = [m for m in (d.get("messages") or []) if isinstance(m, dict)]
    if not msgs:
        msgs = list(((d.get("history") or {}).get("messages") or {}).values())
    rows = c.execute("select role, model_id, content from chat_message where chat_id = ? order by created_at",
                     (cid,)).fetchall()
    out.append({"id": cid, "title": title, "models": d.get("models"),
                "messages": [{"role": r, "model": m, "content": (t or "")[:600]} for r, m, t in rows]
                or [{"role": m.get("role"), "model": m.get("model"), "content": str(m.get("content"))[:600]}
                    for m in msgs]})
print(json.dumps(out))
'''


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(args.out or f"/srv/logs/acceptance/owui-identity-browser-{run_id}")
    out_dir.mkdir(parents=True, exist_ok=True)
    email = f"gx-acceptance-{run_id.lower()}-ui@example.invalid"
    password = secrets.token_urlsafe(24)
    created = acc.owui({"op": "create", "email": email, "password": password,
                        "name": "GX acceptance (temporary)", "models": MODELS})
    uid = created["user_id"]
    report: dict = {"run": run_id, "account": uid, "checks": []}

    def check(label: str, ok: bool, **detail) -> None:
        report["checks"].append({"check": label, "ok": bool(ok), **detail})
        print(("PASS " if ok else "FAIL ") + label + (f"  {json.dumps(detail)[:400]}" if detail else ""), flush=True)

    crashed = None
    try:
        shot = out_dir / "owui-gx-mini-identity.png"
        env = {**os.environ, "GX_OWUI_EMAIL": email, "GX_OWUI_PASSWORD": password, "GX_OWUI_SHOT": str(shot)}
        res = subprocess.run(["node", str(HERE / "owui_identity_browser.mjs")], env=env, capture_output=True,
                             text=True, timeout=400)
        if res.returncode != 0:
            raise RuntimeError(res.stderr.strip()[-800:])
        ui = json.loads(res.stdout.strip().splitlines()[-1])
        report["ui"] = ui
        region = ui.get("answer_region", "")
        check("the chat.legenex.co UI shows gx-mini's answer naming the HauhauCS model",
              acc.FULL.lower() in region.lower() and "gx-mini" in region.lower(), answer=region[:400])
        check("a screenshot was taken", shot.is_file() and shot.stat().st_size > 10_000, path=str(shot))
        listing = subprocess.run(["docker", "exec", "open-webui", "python3", "-c", _CHATS, uid], capture_output=True,
                                 text=True, timeout=30, check=True).stdout
        chats = json.loads(listing)
        report["stored_chats"] = chats
        stored = chats[0] if chats else {}
        answers = [m for m in stored.get("messages", []) if m.get("role") == "assistant"]
        check("Open WebUI stored one new chat for the test account, on model gx-mini",
              len(chats) == 1 and stored.get("models") == ["gx-mini"], chats=len(chats), models=stored.get("models"))
        check("the stored assistant message is from gx-mini and names the underlying model",
              bool(answers) and answers[-1].get("model") == "gx-mini"
              and acc.FULL.lower() in answers[-1].get("content", "").lower(),
              stored_answer=(answers[-1].get("content", "") if answers else "")[:300])
        check("the answer makes no false size / product / precision claim",
              not [p for p in acc.judge(0, answers[-1]["content"]) if "missing" not in p] if answers else False,
              problems=acc.judge(0, answers[-1]["content"]) if answers else None)
    except Exception as exc:  # noqa: BLE001 - a crash is a failed run
        crashed = f"{type(exc).__name__}: {exc}"
        print(crashed, file=sys.stderr)
    finally:
        cleaned = acc.owui({"op": "cleanup", "user_id": uid, "models": MODELS})
        report["cleanup"] = cleaned
        check("test account, its chat and its grants are gone",
              cleaned["deleted"] and not cleaned["user_left"] and cleaned["grants_left"] == 0, **cleaned)
        report["crashed"] = crashed
        report["passed"] = crashed is None and all(c["ok"] for c in report["checks"])
        text = json.dumps(report, indent=2)
        assert password not in text
        (out_dir / "owui-identity-browser.json").write_text(text)
        print(f"\n{'ALL PASS' if report['passed'] else 'FAILURES'}: "
              f"{sum(c['ok'] for c in report['checks'])}/{len(report['checks'])} -> {out_dir}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
