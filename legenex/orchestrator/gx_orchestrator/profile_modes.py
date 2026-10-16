"""AgentOS-owned floor/ceiling on the existing gx-auto classifier.

Only the HMAC-authenticated LiteLLM key alias selects a profile. Caller-sent
profile/agent/priority fields cannot change this policy. AgentOS remains the
one routing store; its existing private management tunnel supplies live bounds.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Callable, Mapping
import urllib.request

from . import modes as M
from .profiles import Registry

PREFIX = 'hermes-profile-'
POLICY_URL = 'http://127.0.0.1:4174/api/mode-policies'


class ProfileModeError(ValueError):
    def __init__(self, message: str, status: int = 403):
        super().__init__(message)
        self.status = status


def fetch_policies() -> Mapping[str, Any]:
    # This is the existing SSH management tunnel, not a caller-controlled URL.
    # No mutation credential or machine-to-owner elevation is involved.
    with urllib.request.urlopen(POLICY_URL, timeout=5) as response:
        body = json.load(response)
    if body.get('ok') is not True or not isinstance(body.get('policies'), dict):
        raise ProfileModeError('AgentOS did not return canonical mode policies')
    return body['policies']


@dataclass(frozen=True)
class BoundedDecision:
    decision: M.ModeDecision
    profile: str
    floor: str
    ceiling: str
    initial_mode: str

    def as_log_dict(self) -> dict:
        return {'agent_profile': self.profile, 'initial_mode': self.initial_mode,
                'escalated_mode': 'gx-' + self.decision.mode,
                'floor': self.floor, 'ceiling': self.ceiling,
                'escalation_reason': self.decision.reason}


def decide(identity: str, alias: str, payload: Mapping[str, Any], registry: Registry,
           fetch: Callable[[], Mapping[str, Any]] | None = None) -> BoundedDecision | None:
    if not identity.startswith(PREFIX):
        return None
    profile = identity[len(PREFIX):]
    if not re.fullmatch(r'[a-z][a-z0-9_-]{0,39}', profile):
        raise ProfileModeError('invalid authenticated profile identity')
    try:
        policies = (fetch or fetch_policies)()
        policy = policies[profile]
        floor, ceiling = policy['floor'], policy['ceiling']
        low = M.MODE_ORDER.index(M.ALIAS_MODE[floor])
        high = M.MODE_ORDER.index(M.ALIAS_MODE[ceiling])
        if low > high:
            raise ValueError('inverted bounds')
    except Exception as exc:
        # Never fall back to an unbounded route during a policy outage.
        raise ProfileModeError('canonical AgentOS policy unavailable or invalid', 503) from exc
    requested = M.ALIAS_MODE.get(alias)
    if requested is not None and not low <= M.MODE_ORDER.index(requested) <= high:
        raise ProfileModeError('requested alias exceeds authenticated profile bounds')
    auto = M.choose_mode(M.ALIAS_AUTO, payload, registry, headers={})
    initial = requested or M.ALIAS_MODE[floor]
    index = max(low, M.MODE_ORDER.index(auto.mode), M.MODE_ORDER.index(initial))
    selected = M.MODE_ORDER[min(high, index)]
    reason = auto.reason + f'; AgentOS {profile} bounds {floor}..{ceiling}'
    if index > high:
        reason += '; task escalation capped by ceiling (context admission still enforced)'
    decision = M.ModeDecision(alias, selected, reason, auto.route)
    return BoundedDecision(decision, profile, floor, ceiling, 'gx-' + initial)
