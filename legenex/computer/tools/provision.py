#!/usr/bin/env python3
"""Idempotent Open WebUI <-> Computer integration on gx10-01 (see ../README.md).

Brings both apps to the documented state without any UI step, through their own APIs:

  LiteLLM      virtual keys `gx-computer` and `open-webui` (public aliases only), 0600 in the
               secrets store. Neither app holds the master key.
  Computer     profile of the canonical Open WebUI account (display name, avatar), LiteLLM
               connection, default model, the GX-Cluster workspace (the empty default
               workspace is retired only when nothing references it), gateway key.
  Open WebUI   LiteLLM connection key, Computer connection (+ conversation headers),
               GX-Cluster folder with project prompt and instructions note, compaction.

Never changes passwords, never deletes users, chats, memories or files. Prints no secrets.

  python3 legenex/computer/tools/provision.py [--email ADDR] [--rotate-gateway-key]
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import uuid

from gxtools import (COMPUTER_URL, HERE, LITELLM_FROM_CPTR, LITELLM_FROM_OWUI, OWUI_COMPUTER_HEADERS,
                     PUBLIC_ALIASES, REPO, SECRETS, WORKSPACE, Computer, ToolError, canonical_identity,
                     cptr_sql, ensure_litellm_key, owui)

WORKSPACE_NAME = "GX-Cluster"
RETIRE_WORKSPACES = ["/home/cptr"]          # cptr's default "home" workspace from the first login
DEFAULT_MODEL = "gx-auto"                   # router alias; /projects/gx-cluster/.cptr/model agrees
CONNECTION_NAME = "GX LiteLLM (local)"
GATEWAY_KEY_NAME = "open-webui"
MODEL_ORDER = ["gx-auto", "gx-mini", "gx-code", "gx-max"]
FOLDER_NAME = "GX-Cluster"
NOTE_TITLE = "GX-Cluster — project instructions"
COMPACTION = {  # sized to the smallest REAL window: gx-mini = 65536 ctx / 2 slots = 32768 per request
    "ENABLE_CONTEXT_COMPACTION": True,
    "CONTEXT_COMPACTION_MODEL": "gx-mini",
    "CONTEXT_COMPACTION_TOKEN_THRESHOLD": 20000,
    "CONTEXT_COMPACTION_TOKEN_CAP": 20000,
    "CONTEXT_COMPACTION_RETENTION_PERCENTAGE": 40,
}
LITELLM_CONN_IF_NEW = {"enable": True, "tags": ["gx"], "prefix_id": "", "model_ids": MODEL_ORDER,
                       "connection_type": "external", "auth_type": "bearer", "passthrough_params": []}
COMPUTER_CONN = {"enable": True, "tags": ["computer"], "prefix_id": "", "model_ids": [],
                 "connection_type": "local", "auth_type": "bearer", "passthrough_params": [],
                 "headers": OWUI_COMPUTER_HEADERS}

report: list[dict] = []


def step(label: str, **kw) -> None:
    report.append({"step": label, **kw})


def expect(ok: bool, what: str) -> None:
    if not ok:
        raise ToolError(what)


def note_markdown() -> str:
    claude = (REPO / "CLAUDE.md").read_text()
    gx = claude.split("\n---\n", 1)[0].rstrip()   # the GX block; the generic community rules follow '---'
    return f"""# {NOTE_TITLE}

Source: `CLAUDE.md` (GX block) in the gx-cluster repository. The live, editable copy is the
Computer workspace **GX-Cluster** (`/projects/gx-cluster` on gx10-01, model `cptr/gx-cluster`);
this note is a reference snapshot for chats in the GX-Cluster folder.

**Live gateway aliases (LiteLLM, `legenex/gateway/litellm/config.yaml`):** `gx-mini`, `gx-code`,
`gx-auto` (router, default), `gx-max`. The L-10 row below predates the gateway retiring
`gx-fast`, `gx-reason`, `gx-image` and `gx-video`; the gateway config is authoritative.

---

{gx}
"""


def provision_computer(ident: dict, cptr_key: str, rotate: bool, owui_has_computer: bool) -> str | None:
    c = Computer(ident["email"])
    expect(c.role == "admin", "the canonical account is not a Computer admin")
    step("computer.identity", username=c.username, role=c.role)

    # profile: same person, same display name and avatar as the canonical Open WebUI account
    name, img = cptr_sql("select display_name, profile_image_url from users where id = ?", (c.user_id,))[0]
    if name != ident["name"]:
        st, _ = c.call("PUT", "/api/auth/profile", {"display_name": ident["name"]})
        expect(st == 200, f"display name update failed ({st})")
    step("computer.display_name", value=ident["name"], changed=name != ident["name"])
    if ident["avatar_b64"] and not img:
        blob, b = base64.b64decode(ident["avatar_b64"]), uuid.uuid4().hex
        ext = ident["avatar_mime"].split("/")[1].split("+")[0]
        body = (f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="avatar.{ext}"\r\n'
                f"Content-Type: {ident['avatar_mime']}\r\n\r\n").encode() + blob + f"\r\n--{b}--\r\n".encode()
        st, _ = c.call("PUT", "/api/auth/avatar", raw=body, ctype=f"multipart/form-data; boundary={b}")
        expect(st == 200, f"avatar upload failed ({st})")
    step("computer.avatar", synced=bool(ident["avatar_b64"]), changed=bool(ident["avatar_b64"] and not img))

    # local inference: LiteLLM on gx_gateway with the least-privilege key, public aliases only
    st, conns = c.call("GET", "/api/admin/connections")
    same = [x for x in (conns or {}).get("connections", []) if x.get("base_url") == LITELLM_FROM_CPTR]
    spec = {"name": CONNECTION_NAME, "api_key": cptr_key, "enabled": True, "models": MODEL_ORDER}
    if same:
        cid = same[0]["id"]
        st, _ = c.call("PUT", f"/api/admin/connections/{cid}", spec)
    else:
        st, r = c.call("POST", "/api/admin/connections",
                       {**spec, "provider": "openai", "api_type": "chat_completions", "base_url": LITELLM_FROM_CPTR})
        cid = (r or {}).get("id")
    expect(st == 200 and cid, f"connection upsert failed ({st})")
    st, v = c.call("POST", f"/api/admin/connections/{cid}/verify")
    expect(st == 200 and (v or {}).get("ok"), f"connection verify failed: {v}")
    step("computer.connection", id=cid, action="updated" if same else "created", verify=v)
    st, _ = c.call("PUT", "/api/admin/config", {"config": {"chat.default_model": DEFAULT_MODEL}})
    expect(st == 200, "default model update failed")
    step("computer.default_model", value=DEFAULT_MODEL)

    # workspace: the real project; retire the meaningless default only when unreferenced
    st, ws = c.call("GET", "/api/state/workspaces")
    paths = [w["path"] for w in ws or []]
    if WORKSPACE not in paths:
        layout = {"name": WORKSPACE_NAME, "activeGroupId": "default",
                  "groups": [{"id": "default", "activeTabId": "files",
                              "tabs": [{"id": "files", "type": "files", "label": "Files", "permanent": True}]}],
                  "layout": {"type": "group", "groupId": "default"}, "splitDirection": "horizontal",
                  "splitRatio": 0.5, "fileBrowserCwd": WORKSPACE}
        st, _ = c.call("PUT", f"/api/state/workspace?path={c.q(WORKSPACE)}", layout)
        expect(st == 200, "workspace registration failed")
    step("computer.workspace", path=WORKSPACE, registered=WORKSPACE not in paths)
    for stale in RETIRE_WORKSPACES:
        if stale not in paths:
            continue
        refs = {"chats": cptr_sql("select count(*) from chats where json_extract(meta,'$.workspace') = ?", (stale,))[0][0],
                "automations": cptr_sql("select count(*) from automations where workspace = ?", (stale,))[0][0]}
        st, listed = c.call("GET", f"/api/chats?workspace={c.q(stale)}&limit=5&offset=0")
        refs["chat_files"] = len((listed or {}).get("chats", []))
        if any(refs.values()):
            step("computer.retire_workspace", path=stale, kept=True, refs=refs)
            continue
        st, _ = c.call("DELETE", f"/api/state/workspace?path={c.q(stale)}")   # DB row only; no files touched
        step("computer.retire_workspace", path=stale, removed=st == 200, refs=refs)
    st, _ = c.call("PUT", "/api/state/preferences", {"workspaceOrder": [WORKSPACE]})
    step("computer.workspace_order", ok=st == 200)

    # gateway key for Open WebUI (acts as this user; shown once, stored hashed by Computer)
    st, keys = c.call("GET", "/v1/keys")
    mine = [k for k in keys or [] if k.get("name") == GATEWAY_KEY_NAME]
    if mine and owui_has_computer and not rotate:
        step("computer.gateway_key", action="kept", count=len(mine))
        return None
    for k in mine:
        c.call("DELETE", f"/v1/keys/{k['id']}")
    st, r = c.call("POST", "/v1/keys", {"name": GATEWAY_KEY_NAME})
    key = (r or {}).get("key")
    expect(st == 200 and bool(key), f"gateway key creation failed ({st})")
    st, models = c.call("GET", "/v1/models", bearer=key)
    ids = [m["id"] for m in (models or {}).get("data", [])]
    expect("cptr/gx-cluster" in ids, f"gateway does not expose the workspace: {ids}")
    step("computer.gateway_key", action="rotated" if mine else "created", revoked_old=len(mine), models=ids)
    return key


def provision_owui(ident: dict, owui_key: str, gateway_key: str | None) -> None:
    ops = [{"openai_upsert": {"url": LITELLM_FROM_OWUI, "key": owui_key, "config": None,
                              "config_if_new": LITELLM_CONN_IF_NEW}}]
    if gateway_key:
        ops.append({"openai_upsert": {"url": COMPUTER_URL, "key": gateway_key, "config": COMPUTER_CONN}})
    for r in owui(ops, ident["email"])[1:]:
        expect(r["status"] == 200, f"Open WebUI connection update failed: {r}")
        step("owui.connection", index=r["index"], action=r["action"])

    # project folder + instructions note (content refreshed from CLAUDE.md)
    notes, folders = [r["body"] for r in owui([{"call": ["GET", "/api/v1/notes/"]},
                                                {"call": ["GET", "/api/v1/folders/"]}], ident["email"])[1:]]
    notes = notes if isinstance(notes, list) else (notes or {}).get("items", [])
    note = next((n for n in notes if n.get("title") == NOTE_TITLE), None)
    md = note_markdown()
    if note:
        r = owui([{"call": ["POST", f"/api/v1/notes/{note['id']}/update",   # validated as NoteForm: title required
                            {"title": NOTE_TITLE, "data": {"content": {"md": md}}}]}], ident["email"])[1]
    else:
        r = owui([{"call": ["POST", "/api/v1/notes/create",
                            {"title": NOTE_TITLE, "data": {"content": {"md": md}}, "access_grants": []}]}], ident["email"])[1]
        note = r["body"]
    expect(r["status"] == 200, f"note upsert failed: {r['status']}")
    step("owui.note", id=note["id"], action="updated" if r["op"].endswith("/update") else "created", chars=len(md))
    data = {"system_prompt": (HERE / "owui_folder_prompt.md").read_text().strip(),
            "files": [{"type": "note", "id": note["id"], "name": NOTE_TITLE}]}
    folder = next((f for f in folders or [] if f.get("name") == FOLDER_NAME and not f.get("parent_id")), None)
    if folder:
        r = owui([{"call": ["POST", f"/api/v1/folders/{folder['id']}/update", {"data": data}]}], ident["email"])[1]
    else:
        r = owui([{"call": ["POST", "/api/v1/folders/", {"name": FOLDER_NAME, "data": data}]}], ident["email"])[1]
        folder = r["body"]
    expect(r["status"] == 200, f"folder upsert failed: {r['status']}")
    step("owui.folder", id=folder["id"], folder=FOLDER_NAME, action="updated" if r["op"].endswith("/update") else "created")

    # context compaction (full-form endpoint: read, change, write back)
    cur = owui([{"call": ["GET", "/api/v1/chats/config"]}], ident["email"])[1]["body"]
    want = {**cur, **COMPACTION, "CONTEXT_COMPACTION_PROMPT_TEMPLATE": (HERE / "compaction_prompt.md").read_text().strip()}
    if want != cur:
        r = owui([{"call": ["POST", "/api/v1/chats/config", want]}], ident["email"])[1]
        expect(r["status"] == 200, f"compaction config failed: {r['status']}")
    step("owui.compaction", changed=want != cur, **COMPACTION)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--email", help="canonical Open WebUI account (default: the only admin)")
    ap.add_argument("--rotate-gateway-key", action="store_true", help="replace the Computer gateway key")
    args = ap.parse_args()
    try:
        ident = canonical_identity(args.email)
        step("canonical_identity", email_domain=ident["email"].split("@")[-1], role=ident["role"])
        a1, cptr_key = ensure_litellm_key("gx-computer", SECRETS / "computer" / "litellm-api-key")
        a2, owui_key = ensure_litellm_key("open-webui", SECRETS / "open-webui" / "litellm-api-key")
        step("litellm.keys", gx_computer=a1, open_webui=a2, aliases=list(PUBLIC_ALIASES))
        has = owui([{"openai_has": COMPUTER_URL}], ident["email"])[1]["present"]
        gateway_key = provision_computer(ident, cptr_key, args.rotate_gateway_key, has)
        provision_owui(ident, owui_key, gateway_key)
        del cptr_key, owui_key, gateway_key
    except ToolError as exc:
        print(json.dumps(report, indent=1))
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
