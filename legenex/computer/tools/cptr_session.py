"""Mint a short-lived Computer (cptr 0.9.21) session. Runs INSIDE gx-computer (copied in by gxtools.py).

Produces the same token POST /api/auth/login issues (HS256 over [server].secret from
/data/config.toml, claims sub/username/role/exp/jti; see cptr/utils/config.py create_token),
but valid for `ttl` seconds instead of 30 days, for an EXISTING user only. Printed to stdout
for the calling process, which must never log it.

stdin: {"username": ..., "ttl": 900}
"""
import json
import sqlite3
import sys
import time
import uuid

import jwt
from cptr.utils.config import _get_jwt_secret

req = json.load(sys.stdin)
db = sqlite3.connect("file:/data/app.db?mode=ro", uri=True)
row = db.execute("select u.id, u.role from users u join auths a on a.user_id = u.id where a.username = ?",
                 (req["username"],)).fetchone()
if not row:
    sys.exit("computer user not found")
ttl = max(60, min(int(req.get("ttl", 900)), 3600))
print(json.dumps({"user_id": row[0], "role": row[1], "token": jwt.encode(
    {"sub": row[0], "username": req["username"], "role": row[1], "exp": time.time() + ttl, "jti": str(uuid.uuid4())},
    _get_jwt_secret(), algorithm="HS256")}))
