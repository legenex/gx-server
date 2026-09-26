#!/usr/bin/env python3
"""Live end-to-end verification of Open WebUI + Computer on gx10-01 (see ../README.md).

Uses the running applications' own APIs and cross-checks every Computer file operation on
the host. Test artifacts live in the gitignored tmp/ area or are deleted through the API,
so nothing reaches the public repo. Read-only for users, chats, memories and settings.

  python3 legenex/computer/tools/verify.py [--email ADDR] [--no-agent] [--compaction]

--compaction additionally runs the long-conversation test on a temporary chat (deleted
afterwards): a conversation larger than gx-mini's real 32768-token window must be refused
without compaction and answered with it.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import time
import uuid

from gxtools import (CPTR_CONTAINER, CPTR_PY, HOST_WORKSPACE, WORKSPACE, Computer, ToolError, canonical_identity,
                     cptr_sql, owui, owui_sql, run)

results: list[tuple[str, str, str]] = []
RO_DIRS = [".git", ".githooks", "ops/git-sync", ".kilo", "legenex/host", "legenex/gateway", "legenex/lifecycle",
           "legenex/scripts", "legenex/media", "legenex/computer"]


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append(("PASS" if ok else "FAIL", name, detail))
    return ok


def host(p: str) -> str:
    return p.replace(WORKSPACE, HOST_WORKSPACE, 1)


def in_cptr(code: str, stdin: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", "exec", "-i", "-w", "/tmp", CPTR_CONTAINER, CPTR_PY, "-c", code],
                          input=stdin, capture_output=True, text=True, timeout=120)


TERMINAL = r'''
import asyncio, json, sys, urllib.request, websockets
tok, cmd, marker = json.load(sys.stdin)
H = {"Cookie": "cptr_session=" + tok, "Content-Type": "application/json"}
def call(m, p, b=None):
    r = urllib.request.Request("http://127.0.0.1:8000" + p, method=m, headers=H,
                               data=None if b is None else json.dumps(b).encode())
    with urllib.request.urlopen(r, timeout=30) as x:
        return json.loads(x.read() or b"null")
sid = call("POST", "/api/terminal", {"rows": 24, "cols": 200, "cwd": "/projects/gx-cluster"})["session_id"]
async def go():
    # cookie header, never ?token=: uvicorn logs the query string
    async with websockets.connect(f"ws://127.0.0.1:8000/api/terminal/{sid}/ws", max_size=None,
                                  additional_headers={"Cookie": "cptr_session=" + tok}) as w:
        await w.send(b"\x00" + cmd.encode())
        buf = b""
        while buf.count(marker.encode()) < 2:
            buf += await asyncio.wait_for(w.recv(), 20)
        return buf.decode(errors="replace")
try:
    out = asyncio.run(go())
finally:
    call("DELETE", f"/api/terminal/{sid}")
print(json.dumps({"out": out[-2000:]}))
'''

PROMPT = r'''
import asyncio, json
from cptr.utils.prompt_templates import load_system_prompt
p = asyncio.run(load_system_prompt("/projects/gx-cluster", model="gx-auto", user_id=None))
print(json.dumps({"len": len(p), "claude": "# CLAUDE.md" in p and "LOCKED" in p,
                  "workspace_rules": "autosyncs and pushes" in p, "tree": "Files:" in p and "legenex/" in p,
                  "raw_placeholders": "{{" in p}))
'''


def verify_computer(c: Computer, nonce: str) -> None:
    head = run(["git", "-C", HOST_WORKSPACE, "rev-parse", "HEAD"]).strip()
    st, me = c.call("GET", "/api/auth")
    check("computer: canonical admin session", st == 200 and me.get("role") == "admin")
    st, ws = c.call("GET", "/api/state/workspaces")
    check("computer: GX-Cluster is the registered workspace", st == 200 and [w["path"] for w in ws] == [WORKSPACE],
          ", ".join(f"{w['name']}={w['path']}" for w in ws or []))
    st, cfg = c.call("GET", "/api/admin/connections")
    conns = (cfg or {}).get("connections", [])
    check("computer: LiteLLM connection enabled", any(x.get("enabled") and x.get("base_url") == "http://gx-litellm:4000/v1" for x in conns))
    st, models = c.call("GET", "/api/chats/models")
    ids = sorted(m["id"] for m in (models or {}).get("models", []))
    check("computer: models are the public aliases, default gx-auto",
          ids == ["gx-auto", "gx-code", "gx-max", "gx-mini"] and (models or {}).get("default") == "gx-auto", str(ids))
    st, listing = c.call("GET", f"/api/workspace/files?path={c.q(WORKSPACE)}")
    names = [e.get("name") for e in (listing or {}).get("entries", [])]
    check("computer: real project files visible", "CLAUDE.md" in names and "legenex" in names, f"{len(names)} entries")
    st, gl = c.call("GET", f"/api/git/log?root={c.q(WORKSPACE)}&limit=1")
    top = ((gl if isinstance(gl, list) else (gl or {}).get("commits", [])) or [{}])[0]
    sha = top.get("hash") or top.get("sha") or ""
    check("computer: git log HEAD equals host HEAD", bool(sha) and head.startswith(sha[:7]), sha[:12])

    made_tmp = not os.path.exists(f"{HOST_WORKSPACE}/tmp")
    d = f"{WORKSPACE}/tmp/cptr-verify-{nonce}"
    f1, f2 = f"{d}/probe.txt", f"{d}/probe-renamed.txt"
    hf = f"{HOST_WORKSPACE}/tmp/term-{nonce}.txt"
    ops = [
        ("fs: create directory", lambda: c.call("POST", "/api/workspace/files/create", {"path": d, "type": "directory"}), lambda: os.path.isdir(host(d))),
        ("fs: create file", lambda: c.call("POST", "/api/workspace/files/create", {"path": f1, "type": "file"}), lambda: os.path.isfile(host(f1))),
        ("fs: write, seen on host", lambda: c.call("POST", "/api/workspace/files/write", {"path": f1, "content": f"alpha {nonce}\n"}),
         lambda: open(host(f1)).read() == f"alpha {nonce}\n"),
        ("fs: read back", lambda: c.call("GET", f"/api/workspace/files/read?path={c.q(f1)}"), None),
        ("fs: edit, seen on host", lambda: c.call("POST", "/api/workspace/files/write", {"path": f1, "content": f"alpha {nonce}\nbeta\n"}),
         lambda: open(host(f1)).read().endswith("beta\n")),
        ("fs: rename, seen on host", lambda: c.call("POST", "/api/workspace/files/move", {"source": f1, "destination": f2}),
         lambda: not os.path.exists(host(f1)) and os.path.isfile(host(f2))),
        ("fs: delete, seen on host", lambda: c.call("POST", "/api/workspace/files/delete", {"path": f2}), lambda: not os.path.exists(host(f2))),
        ("fs: remove test directory", lambda: c.call("POST", "/api/workspace/files/delete", {"path": d}), lambda: not os.path.exists(host(d))),
    ]
    try:
        for name, act, on_host in ops:
            st, body = act()
            ok = st == 200 and (on_host() if on_host else (body or {}).get("content") == f"alpha {nonce}\n")
            if not check(name, ok, f"HTTP {st}"):
                break
        r = in_cptr(TERMINAL, json.dumps([c.secret_token, f"git rev-parse --show-toplevel; git rev-parse HEAD; "
                                                              f"echo T-{nonce} > tmp/term-{nonce}.txt; echo DONE-{nonce}\n", f"DONE-{nonce}"]))
        out = json.loads(r.stdout).get("out", "") if r.returncode == 0 else ""
        check("terminal: shell runs in the workspace and sees the same git HEAD",
              "/projects/gx-cluster" in out and head in out, "" if out else r.stderr.strip()[-200:])
        check("terminal: file written in terminal is on the host",
              os.path.exists(hf) and open(hf).read().strip() == f"T-{nonce}")
    finally:
        shutil.rmtree(host(d), ignore_errors=True)
        if os.path.exists(hf):
            os.remove(hf)
        if made_tmp and os.path.isdir(f"{HOST_WORKSPACE}/tmp") and not os.listdir(f"{HOST_WORKSPACE}/tmp"):
            os.rmdir(f"{HOST_WORKSPACE}/tmp")
    check("fs+terminal: test files removed", not os.path.exists(host(d)) and not os.path.exists(hf))

    r = in_cptr(PROMPT, "")
    p = json.loads(r.stdout) if r.returncode == 0 else {}
    check("instructions: CLAUDE.md + workspace rules + file tree in the system prompt",
          p.get("claude") and p.get("workspace_rules") and p.get("tree") and not p.get("raw_placeholders"),
          f"{p.get('len')} chars")
    st, _ = c.call("GET", "/v1/models", cookie=False)
    check("gateway: unauthenticated request refused", st == 401, f"HTTP {st}")

    # read-only overlays (docker-compose.computer.yml): mounted, enforced, and the gateway secret unreadable
    probe = ("cut -d' ' -f5 /proc/self/mountinfo; "
             "for d in " + " ".join(RO_DIRS) + "; do if touch $d/.ro-probe 2>/dev/null; then rm -f $d/.ro-probe; echo WRITABLE $d; fi; done; "
             "if [ -w .gitignore ]; then echo WRITABLE .gitignore; fi; "
             "if cat legenex/gateway/.env >/dev/null 2>&1; then echo READABLE legenex/gateway/.env; fi")
    out = run(["docker", "exec", "-w", WORKSPACE, CPTR_CONTAINER, "sh", "-c", probe]).splitlines()
    mounted = set(out)
    missing = [p for p in RO_DIRS + [".gitignore"] if f"{WORKSPACE}/{p}" not in mounted]
    check("overlays: every read-only overlay is mounted (recreate gx-computer if not)", not missing, " ".join(missing))
    bad = [l for l in out if l.startswith(("WRITABLE", "READABLE"))]
    check("overlays: protected paths read-only; gateway .env unreadable inside Computer", not bad, " ".join(bad))


def verify_owui(email: str | None, agent: bool, c: Computer) -> None:
    ident = canonical_identity(email)
    check("owui: canonical account intact", ident["role"] == "admin" and bool(ident["name"]))
    cfg = dict(owui_sql("select key, value from config where key in ('folders.enable','notes.enable','memories.enable',"
                        "'memories.system_context.enable','memories.background_review.enable','memories.review_interval_turns',"
                        "'memories.user_char_limit','memories.context_char_limit','chat.context_compaction.enable')"))
    check("owui: folders, notes, memories, system context, background review, compaction enabled",
          all(json.loads(cfg.get(k, "false")) is True for k in ("folders.enable", "notes.enable", "memories.enable",
              "memories.system_context.enable", "memories.background_review.enable", "chat.context_compaction.enable")),
          f"review every {cfg.get('memories.review_interval_turns')} turns, budgets "
          f"{cfg.get('memories.user_char_limit')}/{cfg.get('memories.context_char_limit')} chars")
    r = owui([{"call": ["GET", "/api/models?refresh=true"]}, {"call": ["GET", "/api/v1/folders/"]}], email)
    ids = [m["id"] for m in r[1]["body"]["data"]]
    check("owui: admin sees the GX aliases and the Computer workspace",
          all(a in ids for a in ("gx-auto", "gx-mini", "gx-code", "gx-max", "cptr/gx-cluster")), str(ids))
    check("owui: GX-Cluster project folder exists", any(f["name"] == "GX-Cluster" for f in r[2]["body"]))
    rows = owui_sql("select count(*) from model where id like 'cptr/%' or base_model_id like 'cptr/%'")[0][0]
    check("owui: no model entry or preset exposes cptr/* (keeps it admin-only)", rows == 0, f"{rows} row(s)")
    others = owui_sql("select email from user where role != 'admin' limit 1")
    if others:
        r = owui([{"call": ["GET", "/api/models"]},
                  {"call": ["POST", "/api/chat/completions", {"model": "cptr/gx-cluster", "stream": False,
                                                              "messages": [{"role": "user", "content": "ping"}]}]}],
                 others[0][0], require_admin=False)
        check("owui: non-admin can neither see nor call cptr/gx-cluster",
              "cptr/gx-cluster" not in [m["id"] for m in r[1]["body"]["data"]] and r[2]["status"] >= 400, f"HTTP {r[2]['status']}")
    r = owui([{"call": ["POST", "/api/chat/completions", {"model": "gx-mini", "stream": False,
                                                          "messages": [{"role": "user", "content": "Reply with exactly: OK-GX"}]}]}], email)[1]
    txt = (r["body"] or {}).get("choices", [{}])[0].get("message", {}).get("content", "") if isinstance(r["body"], dict) else ""
    check("owui: normal inference (gx-mini via LiteLLM)", r["status"] == 200 and "OK-GX" in txt, txt.strip()[:40])
    if not agent:
        return
    tag = uuid.uuid4().hex[:10]
    t_start = time.time() - 1
    r = owui([{"call": ["POST", "/api/chat/completions", {"model": "cptr/gx-cluster", "stream": False, "messages": [{"role": "user", "content":
        f"Verification run {tag}: do not create, modify or delete any file. Run `echo VERIFY-{tag}; git rev-parse --abbrev-ref HEAD` "
        "in the workspace, then answer on two lines: 'BRANCH: <name>' and 'L-8: <what locked constraint L-8 in the project "
        "instructions requires>'."}]}], "timeout": 900}], email)[1]
    txt = (r["body"] or {}).get("choices", [{}])[0].get("message", {}).get("content", "") if isinstance(r["body"], dict) else str(r["body"])
    check("owui -> Computer gateway -> agent -> LiteLLM (ran git, read the project instructions)",
          r["status"] == 200 and "main" in txt and "swapfile-sglang" in txt, txt.strip().replace("\n", " / ")[:160])
    # any non-ignored file written during the run (mtime), immune to autosync committing meanwhile
    listed = run(["git", "-C", HOST_WORKSPACE, "ls-files", "-com", "--exclude-standard", "-z"]).split("\0")
    touched = sorted({f for f in listed if f and os.path.exists(f"{HOST_WORKSPACE}/{f}")
                      and os.path.getmtime(f"{HOST_WORKSPACE}/{f}") >= t_start})
    check("agent: no project file written by the read-only agent run", not touched, " ".join(touched[:5]))
    # remove only this run's chat(s) and task log(s): identified by the tag, never by time
    c = Computer(c.username, ttl=900)   # fresh session: the agent call may have outlived the first one
    tagged = [row[0] for row in cptr_sql("select distinct chat_id from chat_messages where content like ?", (f"%{tag}%",))]
    for chat_id in tagged:
        st, _ = c.call("DELETE", f"/api/chats/{chat_id}")   # 404 = already removed with its parent
    left = cptr_sql("select count(*) from chat_messages where content like ?", (f"%{tag}%",))[0][0]
    check("cleanup: this run's Computer chat deleted (and only it)", bool(tagged) and left == 0, f"{len(tagged)} chat(s)")
    logs = f"{HOST_WORKSPACE}/.cptr/task_logs"
    for f in os.listdir(logs) if os.path.isdir(logs) else []:
        try:
            if f"VERIFY-{tag}" in open(f"{logs}/{f}", errors="replace").read():
                os.remove(f"{logs}/{f}")
        except OSError:
            pass
    for d in ("chats", "task_logs"):   # Computer recreates these on demand
        p = f"{HOST_WORKSPACE}/.cptr/{d}"
        if os.path.isdir(p) and not os.listdir(p):
            os.rmdir(p)


def verify_compaction(email: str | None) -> None:
    nonce = uuid.uuid4().hex[:8]
    canary, rnd = f"PELICAN-{nonce}", random.Random(nonce)
    words = "scheduler lease drain swap ramp window budget replica shard queue tensor checkpoint warmup kernel fabric route".split()

    def prose(n: int) -> str:
        s = []
        while sum(len(x) + 1 for x in s) < n:
            s.append(f"The operator reviewed how the {rnd.choice(words)} behaved on gx10-0{rnd.randint(1, 2)} and noted that "
                     f"the {rnd.choice(words)} stayed within range while the team discussed whether the {rnd.choice(words)} "
                     f"should change before the next maintenance window, then kept the settings for {rnd.randint(5, 90)} minutes.")
        return " ".join(s)[:n]

    msgs, parent = [], None
    for i in range(23):   # 23 prior user turns + the question = 24: not a memory-review multiple of 10
        u, a = str(uuid.uuid4()), str(uuid.uuid4())
        msgs.append({"id": u, "parentId": parent, "childrenIds": [a], "role": "user", "timestamp": int(time.time()),
                     "content": (f"Note for later: the maintenance code word is {canary}. " if i == 0 else "") + prose(3900)})
        msgs.append({"id": a, "parentId": u, "childrenIds": [], "role": "assistant", "model": "gx-mini", "done": True,
                     "timestamp": int(time.time()), "content": f"Acknowledged {i}. " + prose(3900)})
        parent = a
    for j in range(len(msgs) - 1):
        msgs[j]["childrenIds"] = [msgs[j + 1]["id"]]
    q = "What is the maintenance code word I gave you in my very first message? Reply with the code word only."
    plain = [{"role": m["role"], "content": m["content"]} for m in msgs] + [{"role": "user", "content": q}]
    ctrl = owui([{"call": ["POST", "/api/chat/completions", {"model": "gx-mini", "messages": plain, "stream": False}]}], email)[1]
    check("compaction: control without compaction exceeds gx-mini's real window",
          ctrl["status"] >= 400 and "context" in json.dumps(ctrl["body"]).lower(),
          str(ctrl["body"])[:120])
    chat = owui([{"call": ["POST", "/api/v1/chats/new", {"chat": {"title": f"ZZ-TEMP compaction verify {nonce}",
            "models": ["gx-mini"], "messages": msgs, "history": {"messages": {m["id"]: m for m in msgs},
                                                                "currentId": msgs[-1]["id"]}}}]}], email)[1]["body"]
    try:
        qid, rid = str(uuid.uuid4()), str(uuid.uuid4())
        owui([{"call": ["POST", "/api/chat/completions", {"model": "gx-mini", "chat_id": chat["id"], "id": rid,
               "parent_id": parent, "stream": False,
               "user_message": {"id": qid, "parentId": parent, "childrenIds": [rid], "role": "user", "content": q,
                                "timestamp": int(time.time()), "models": ["gx-mini"]},
               "messages": [{"id": m["id"], "role": m["role"], "content": m["content"]} for m in msgs]
                           + [{"id": qid, "role": "user", "content": q}]}], "timeout": 900}], email)
        msg = {}
        for _ in range(120):
            got = owui([{"call": ["GET", f"/api/v1/chats/{chat['id']}"]}], email)[1]["body"]
            msg = ((got.get("chat") or {}).get("history") or {}).get("messages", {}).get(rid) or {}
            if msg.get("done") and (msg.get("content") or msg.get("error")):
                break
            time.sleep(3)
        usage = msg.get("usage") or {}
        summ = owui_sql("select length(context_summary), instr(context_summary, ?) > 0 from chat_message "
                        "where chat_id = ? and context_summary is not null", (canary, chat["id"]))
        check("compaction: summary checkpoint stored and preserves early facts", bool(summ) and summ[0][1] == 1,
              f"summary {summ[0][0] if summ else 0} chars")
        check("compaction: compacted request within the window and answered",
              0 < (usage.get("prompt_tokens") or 0) < 32768 and canary in (msg.get("content") or ""),
              f"prompt_tokens={usage.get('prompt_tokens')} answer={str(msg.get('content'))[:30]!r}")
    finally:
        d = owui([{"call": ["DELETE", f"/api/v1/chats/{chat['id']}"]}], email)[1]
        check("cleanup: temporary chat deleted", d["status"] == 200)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--email", help="canonical Open WebUI account (default: the only admin)")
    ap.add_argument("--no-agent", action="store_true", help="skip the Computer agent run through Open WebUI")
    ap.add_argument("--compaction", action="store_true", help="also run the long-conversation compaction test")
    args = ap.parse_args()
    try:
        ident = canonical_identity(args.email)
        c = Computer(ident["email"], ttl=1800)
        check("identity: Computer has exactly one user, the canonical admin",
              cptr_sql("select count(*) from users")[0][0] == 1 and c.role == "admin")
        check("identity: Computer display name matches Open WebUI", cptr_sql(
            "select display_name from users where id = ?", (c.user_id,))[0][0] == ident["name"])
        verify_computer(c, uuid.uuid4().hex[:10])
        verify_owui(args.email, not args.no_agent, c)
        if args.compaction:
            verify_compaction(args.email)
    except (ToolError, KeyError, ValueError) as exc:
        check("verification completed", False, f"{type(exc).__name__}: {exc}")
    for s, name, detail in results:
        print(f"{s}  {name}" + (f"  [{detail}]" if detail else ""))
    failed = [r for r in results if r[0] == "FAIL"]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
