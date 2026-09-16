"""Secret masking for anything that leaves the backend: logs, command output,
upstream error bodies.

Two layers: exact values of the secrets this process knows about (always
masked, whatever the surrounding text looks like), then generic patterns for
credentials that merely look like credentials.
"""

from __future__ import annotations

import re

from .config import secret_values

MASK = "[REDACTED]"

_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Authorization: Bearer <token> / Basic <token>
    re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer|basic|token)\s+)[^\s\"',;]+"),
    re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=\-]{8,}"),
    # key=value / "key": "value" for credential-ish keys
    re.compile(
        r"(?i)((?:api[_-]?key|apikey|secret|password|passwd|pwd|token|master[_-]?key|"
        r"access[_-]?key|private[_-]?key|client[_-]?secret|auth)[\"']?\s*[:=]\s*[\"']?)"
        r"(?!\[REDACTED\])[^\s\"',;&]{4,}"
    ),
    # OpenAI/LiteLLM style keys, GitHub tokens, HF tokens, AWS keys, Slack tokens
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bhf_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    # credentials embedded in URLs: scheme://user:pass@host
    re.compile(r"(?i)(\b[a-z][a-z0-9+.\-]*://[^\s/:@]+:)[^\s/@]+(@)"),
    # PEM private key bodies
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
)


def redact(text: str) -> str:
    if not text:
        return text
    for value in secret_values():
        if value in text:
            text = text.replace(value, MASK)
    for pattern in _PATTERNS:
        if pattern.groups == 0:
            text = pattern.sub(MASK, text)
        elif pattern.groups == 1:
            text = pattern.sub(lambda m: m.group(1) + MASK, text)
        else:
            text = pattern.sub(lambda m: m.group(1) + MASK + m.group(2), text)
    return text


#: Keys whose value is a credential. `token` only as a whole word or suffix
#: (`access_token`), so usage counters like `max_tokens` stay visible.
_CRED_KEY = re.compile(
    r"(?i)(api[_-]?key|secret|password|passwd|authorization|master[_-]?key|cookie|(?:^|[_-])token$)"
)


def is_credential_key(key: str) -> bool:
    return bool(_CRED_KEY.search(key))


def redact_obj(obj):
    """Recursively redact strings inside JSON-like data; mask credential keys."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, list):
        return [redact_obj(v) for v in obj]
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if isinstance(key, str) and is_credential_key(key):
                out[key] = MASK
            else:
                out[key] = redact_obj(value)
        return out
    return obj
