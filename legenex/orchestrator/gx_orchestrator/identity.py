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
* ``may_set_priority`` whether its X-GX-Priority header may RAISE priority
  above the mode default (capped by max_priority); lowering is always
  honoured, on every deployment.
* ``may_raise_reasoning`` whether X-GX-Reasoning may go above the mode's level.
* ``may_set_profile`` whether its X-GX-Profile may cycle the V4.1 engine on
  gx-max (default no: a profile change restarts both nodes).
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
from typing import Any, Callable, Mapping

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
    #: May an X-GX-Profile header cycle the legacy (V4.1) engine on gx-max?
    may_set_profile: bool = False
    #: May X-GX-Reasoning go ABOVE the mode's level (lowering is always allowed)?
    may_raise_reasoning: bool = False


@dataclass(frozen=True)
class IdentityPolicy:
    default: IdentityRule = field(default_factory=IdentityRule)
    identities: dict[str, IdentityRule] = field(default_factory=dict)

    def rule(self, identity: str) -> IdentityRule:
        return self.identities.get(identity, self.default)

    def resolve(self, identity: str, mode_priority: str, header_priority: str = "",
                *, legacy: bool = False) -> tuple[str, str]:
        """(scheduler priority, why) for one request -- the same rule on every
        deployment (owner requirement, mission §9: "do not trust arbitrary
        caller-supplied priority headers"):

        * the base is the identity's pinned class (only odin: health) or the
          mode's default;
        * a header that LOWERS it is always honoured (bench scripts send
          `background`; asking for less gains nothing);
        * a header that RAISES it needs a signed identity with
          `may_set_priority`, and never beyond `max_priority`;
        * nobody reaches the health lane by header.
        `legacy` is accepted for callers' clarity; it no longer changes the rule.
        """
        rule = self.rule(identity)
        who = identity or "anonymous"
        if rule.priority:
            prio, why = rule.priority, f"identity {who} is pinned to {rule.priority}"
        else:
            prio, why = mode_priority, f"mode default {mode_priority}"
        asked = (header_priority or "").strip().lower()
        canon = SCHED.valid_priority(asked) if asked and asked != "health" else ""
        if asked and not canon:
            return prio, why + f"; X-GX-Priority {asked!r} is not a settable class"
        if not canon or canon == prio:
            return prio, why
        if _rank(canon) > _rank(prio):
            return canon, f"X-GX-Priority {canon} (lowering is always allowed)"
        if not rule.may_set_priority:
            return prio, why + f"; X-GX-Priority {canon!r} ignored ({who} may not raise priority)"
        cap = rule.max_priority
        if _rank(canon) < _rank(cap):
            return cap, f"X-GX-Priority {canon} from {who}, capped at {cap}"
        return canon, f"X-GX-Priority {canon} from {who}"

    def resolve_reasoning(self, identity: str, base_level: str, header_level: str,
                          rank: "Callable[[str], int | None]") -> tuple[str, str]:
        """(reasoning level, why): the same raise/lower rule as priority.

        `rank(level)` orders levels by reasoning depth (None = not a level).
        Lowering below the mode's level is always honoured; raising needs a
        signed identity with `may_raise_reasoning`. An unknown header value is
        returned as is so the caller answers it with a 400.
        """
        asked = (header_level or "").strip()
        if not asked:
            return base_level, f"mode level {base_level}"
        r_asked, r_base = rank(asked), rank(base_level)
        if r_asked is None:
            return asked, "unknown X-GX-Reasoning (400)"
        if r_base is None or r_asked <= r_base:
            return asked, f"X-GX-Reasoning {asked} (not above the mode's {base_level})"
        who = identity or "anonymous"
        if self.rule(identity).may_raise_reasoning:
            return asked, f"X-GX-Reasoning {asked} from {who}"
        return base_level, f"X-GX-Reasoning {asked!r} ignored ({who} may not raise reasoning above {base_level})"


def reasoning_ranker(registry: Any) -> "Callable[[str], int | None]":
    """Order reasoning levels by effort: thinking off = 0, else reasoning_effort;
    a bare integer 1-100 is its own effort (V4.1 numeric levels)."""
    def rank(level: str) -> "int | None":
        text = str(level).strip().lower()
        if text.isdigit():
            lo, hi = registry.reasoning.numeric_range
            return int(text) if lo <= int(text) <= hi else None
        kw = registry.reasoning.mapping.get(text)
        if kw is None:
            return None
        if kw.get("enable_thinking") is False or kw.get("thinking") is False:
            return 0
        effort = kw.get("reasoning_effort")
        return int(effort) if isinstance(effort, (int, float)) else 50
    return rank


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
    return IdentityRule(priority=prio, max_priority=cap, may_set_priority=bool(raw.get("may_set_priority")),
                        may_set_profile=bool(raw.get("may_set_profile")),
                        may_raise_reasoning=bool(raw.get("may_raise_reasoning")))


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
