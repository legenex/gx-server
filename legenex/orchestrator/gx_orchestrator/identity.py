"""Scheduler priority from the AUTHENTICATED caller, not from what it claims (D-053).

The LiteLLM hook stamps ``X-GX-Identity`` with the key alias LiteLLM itself
authenticated (a client-sent X-GX-Identity is overwritten there) and signs
it with GX_IDENTITY_HMAC_KEY, a secret only the gateway and the orchestrator
hold; an unsigned or stale identity counts as anonymous.

Policy file (JSON, no secrets; default ``identity-policy.json`` next to this
package, override with GX_IDENTITY_POLICY)::

    {"default":    {"max_priority": "interactive"},
     "identities": {"open-webui": {"priority": "interactive"},
                    "odin":       {"priority": "health"},
                    "ai-os":      {"max_priority": "orchestrator", "may_set_priority": true}}}

* ``priority``      the class this identity always gets (wins over the mode);
* ``max_priority``  the best class it may reach (a mode's default is capped);
* ``may_set_priority`` whether its X-GX-Priority header is honoured at all
  (still capped by max_priority). Everyone else's header is ignored.
* ``health`` is the reserved Odin lane: only an identity whose ``priority``
  is health gets it; nobody can ask for it by header unless it may reach it.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from . import scheduler as SCHED

DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[1] / "identity-policy.json"
_NAME_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.@:")


def _rank(name: str) -> int:
    return SCHED.PRIORITIES.get(name, 99)


@dataclass(frozen=True)
class IdentityRule:
    priority: str = ""
    max_priority: str = "interactive"
    may_set_priority: bool = False


@dataclass(frozen=True)
class IdentityPolicy:
    default: IdentityRule = field(default_factory=IdentityRule)
    identities: dict[str, IdentityRule] = field(default_factory=dict)

    def rule(self, identity: str) -> IdentityRule:
        return self.identities.get(identity, self.default)

    def resolve(self, identity: str, mode_priority: str, header_priority: str = "",
                *, legacy: bool = False) -> tuple[str, str]:
        """(scheduler priority, why) for one request.

        * A header that LOWERS priority is always honoured (bench scripts send
          `background`; nobody gains anything by asking for less).
        * `legacy=True` (a deployment without mode routing, i.e. the V4.1
          rollback): the pre-D-053 contract -- a valid X-GX-Priority wins, so
          `X-GX-Priority: interactive` still wakes an idle-released engine.
          Only the health lane stays out of reach of a header.
        * Otherwise raising needs `may_set_priority`, and never beyond the cap.
        """
        rule = self.rule(identity)
        who = identity or "anonymous"
        if legacy:
            asked = (header_priority or "").strip().lower()
            canon = SCHED.valid_priority(asked) if asked and asked != "health" else ""
            if canon:
                return canon, f"X-GX-Priority {canon} (legacy deployment: header wins)"
            if rule.priority == "health":
                return "health", f"identity {who} is pinned to the health lane"
            return mode_priority, f"mode default {mode_priority} (legacy deployment)"
        if rule.priority:
            prio, why = rule.priority, f"identity {who} is pinned to {rule.priority}"
        else:
            prio, why = mode_priority, f"mode default {mode_priority}"
        asked = (header_priority or "").strip().lower()
        canon = SCHED.valid_priority(asked) if asked and asked != "health" else ""
        if asked and not canon:
            why += f"; X-GX-Priority {asked!r} is not a settable class"
        if canon:
            if _rank(canon) >= _rank(prio):
                return canon, f"X-GX-Priority {canon} (lowering is always allowed)"
            if not rule.may_set_priority:
                why += f"; X-GX-Priority {canon!r} ignored ({who} may not raise it)"
            else:
                prio, why = canon, f"X-GX-Priority {canon} from {who}"
        cap = rule.priority or rule.max_priority
        if _rank(prio) < _rank(cap):
            why += f"; capped at {cap}"
            prio = cap
        if prio not in SCHED.PRIORITIES:
            prio, why = SCHED.DEFAULT_PRIORITY, why + "; unknown class -> default"
        return prio, why


def _rule(raw: Mapping[str, Any], where: str) -> IdentityRule:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{where}: must be an object")
    prio = str(raw.get("priority") or "")
    cap = str(raw.get("max_priority") or (prio or "interactive"))
    for value, key in ((prio, "priority"), (cap, "max_priority")):
        if value and value not in SCHED.PRIORITIES:
            raise ValueError(f"{where}.{key}: unknown priority class {value!r}")
    if cap == "health" and prio != "health":
        raise ValueError(f"{where}: only an identity pinned to priority 'health' may reach the health lane")
    return IdentityRule(priority=prio, max_priority=cap, may_set_priority=bool(raw.get("may_set_priority")))


def parse_policy(doc: Mapping[str, Any]) -> IdentityPolicy:
    ids = doc.get("identities") or {}
    if not isinstance(ids, Mapping):
        raise ValueError("identities: must be an object")
    return IdentityPolicy(
        default=_rule(doc.get("default") or {}, "default"),
        identities={str(k): _rule(v, f"identities.{k}") for k, v in ids.items()},
    )


def load_policy(path: "str | os.PathLike[str] | None" = None) -> IdentityPolicy:
    """The policy file; a missing file means "everyone default" (fail closed:
    nobody may set priority, nobody gets the health lane)."""
    p = Path(path or os.environ.get("GX_IDENTITY_POLICY") or DEFAULT_POLICY_PATH)
    try:
        return parse_policy(json.loads(p.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return IdentityPolicy()


#: Seconds a gateway identity signature stays valid (bounds replay).
SIGNATURE_MAX_AGE = 300


def sign_identity(key: str, identity: str, ts: int, nonce: str) -> str:
    """HMAC-SHA256 over "identity|ts|nonce" (the gateway hook computes the same)."""
    return hmac.new(key.encode(), f"{identity}|{ts}|{nonce}".encode(), hashlib.sha256).hexdigest()


class NonceCache:
    """Nonces seen within the signature window: a signed identity is single-use."""

    def __init__(self, ttl: float = SIGNATURE_MAX_AGE * 2, cap: int = 100_000) -> None:
        self._ttl = ttl
        self._cap = cap
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def first_use(self, nonce: str, now: float) -> bool:
        with self._lock:
            if len(self._seen) > self._cap or (self._seen and len(self._seen) % 1024 == 0):
                self._seen = {n: t for n, t in self._seen.items() if t > now}
            if self._seen.get(nonce, 0.0) > now:
                return False
            self._seen[nonce] = now + self._ttl
            return True


_NONCE_OK = set("0123456789abcdef")


def identity_from_headers(headers: Any, key: "str | None" = None, *, now: "float | None" = None,
                          nonces: "NonceCache | None" = None) -> str:
    """The gateway-stamped identity, or "" when it is not proven.

    The hook signs ``X-GX-Identity`` with a key only it and the orchestrator
    hold, over the identity, a timestamp and a per-request random nonce
    (``X-GX-Identity-Ts`` / ``-Nonce`` / ``-Sig``). A holder of the
    orchestrator API key can therefore neither forge an identity (e.g. the
    odin lane) nor replay a captured one: the nonce is accepted once
    (`nonces`) and only within SIGNATURE_MAX_AGE seconds.
    Without a configured key no identity is trusted (fail closed: default rule).
    """
    get = (lambda k: headers.get(k)) if headers is not None else (lambda k: None)
    value = str(get("X-GX-Identity") or "").strip()
    if not value or len(value) > 96 or not set(value) <= _NAME_OK or not key:
        return ""
    try:
        ts = int(str(get("X-GX-Identity-Ts") or "").strip())
    except ValueError:
        return ""
    clock = now if now is not None else time.time()
    if abs(clock - ts) > SIGNATURE_MAX_AGE:
        return ""
    nonce = str(get("X-GX-Identity-Nonce") or "").strip().lower()
    if not 16 <= len(nonce) <= 64 or not set(nonce) <= _NONCE_OK:
        return ""
    sig = str(get("X-GX-Identity-Sig") or "").strip()
    if not sig or not hmac.compare_digest(sig, sign_identity(key, value, ts, nonce)):
        return ""
    if nonces is not None and not nonces.first_use(nonce, clock):
        return ""
    return value
