"""Scheduler priority from the AUTHENTICATED caller, not from what it claims (D-053).

The LiteLLM hook stamps ``X-GX-Identity`` with the key alias LiteLLM itself
authenticated (a client-sent X-GX-Identity is overwritten there). The
orchestrator only accepts requests bearing its own key, which only the
gateway and operators hold, so the header is trustworthy here.

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

import json
import os
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

    def resolve(self, identity: str, mode_priority: str, header_priority: str = "") -> tuple[str, str]:
        """(scheduler priority, why) for one request."""
        rule = self.rule(identity)
        who = identity or "anonymous"
        if rule.priority:
            prio, why = rule.priority, f"identity {who} is pinned to {rule.priority}"
        else:
            prio, why = mode_priority, f"mode default {mode_priority}"
        asked = (header_priority or "").strip().lower()
        if asked:
            canon = SCHED.valid_priority(asked) if asked != "health" else "health"
            if not rule.may_set_priority:
                why += f"; X-GX-Priority {asked!r} ignored ({who} may not set it)"
            elif canon in SCHED.PRIORITIES:
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


def identity_from_headers(headers: Any) -> str:
    """The gateway-stamped identity (bounded, charset-checked) or ""."""
    value = str((headers.get("X-GX-Identity") if headers is not None else "") or "").strip()
    if not value or len(value) > 96 or not set(value) <= _NAME_OK:
        return ""
    return value
