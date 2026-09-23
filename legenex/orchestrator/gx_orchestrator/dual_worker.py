"""gx-max as a coordinated two-worker workflow.

Solver on one gx-code worker, independent reviewer on the other, then repair
and synthesis. Does not start DeepSeek SGLang. Occupies both code workers
while running.

Streaming is not used: correctness is preferred over first-token latency.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from .upstream import UpstreamError, open_post

log = logging.getLogger("gx.dual_worker")

SOLVER_SYSTEM = (
    "You are the SOLVER for gx-max. Inspect the request carefully. Implement or "
    "answer completely. Do not claim completion without evidence. List files you "
    "would change and the exact reasoning."
)
REVIEWER_SYSTEM = (
    "You are an INDEPENDENT REVIEWER for gx-max. You did not write the solver's "
    "work. Inspect the original requirements and the solver output. Look for "
    "wrong assumptions, bugs, regressions, security issues, architecture "
    "violations, unnecessary complexity, unmet requirements, missing tests, "
    "fake completion claims, edge cases, and integration failures. "
    "Reply with JSON: {\"verdict\": \"pass\"|\"fail\", \"defects\": [\"...\"], "
    "\"notes\": \"...\"}. Do not praise. Do not implement yet."
)
REPAIR_SYSTEM = (
    "You are the SOLVER repairing gx-max work after an independent review. "
    "Fix every listed defect. Do not argue. Produce the corrected answer."
)


def _with_system(system: str, messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """Single leading system message. Qwen/Ornith templates reject a second system turn."""
    extras: list[str] = []
    rest: list[dict[str, str]] = []
    for msg in messages:
        if msg.get("role") == "system":
            extras.append(str(msg.get("content") or ""))
        else:
            rest.append(msg)
    combined = system
    if extras:
        combined = system + "\n\n" + "\n\n".join(x for x in extras if x.strip())
    return [{"role": "system", "content": combined}, *rest]


def _content(resp: Mapping[str, Any]) -> str:
    choices = resp.get("choices") or []
    if not choices:
        return json.dumps(resp)[:4000]
    msg = (choices[0] or {}).get("message") or {}
    return str(msg.get("content") or "")


def _chat(url: str, api_key: str, model: str, messages: list[dict[str, str]], timeout: int) -> dict[str, Any]:
    body = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "stream": False,
        "max_tokens": 4096,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    with open_post(url, body, headers=headers, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8"))


def run_workflow(
    *,
    gateway_chat_url: str,
    api_key: str,
    original: Mapping[str, Any],
    solver_model: str = "gx-code-01",
    reviewer_model: str = "gx-code-02",
    timeout: int = 240,
) -> dict[str, Any]:
    user_messages = list(original.get("messages") or [])
    solver = _chat(
        gateway_chat_url,
        api_key,
        solver_model,
        _with_system(SOLVER_SYSTEM, user_messages),
        timeout,
    )
    solver_text = _content(solver)
    review = _chat(
        gateway_chat_url,
        api_key,
        reviewer_model,
        _with_system(
            REVIEWER_SYSTEM,
            [
                *user_messages,
                {"role": "assistant", "content": solver_text[:12000]},
                {"role": "user", "content": "Review the solver output independently."},
            ],
        ),
        timeout,
    )
    review_text = _content(review)
    verdict = "fail"
    try:
        parsed = json.loads(review_text[review_text.find("{") : review_text.rfind("}") + 1])
        verdict = str(parsed.get("verdict") or "fail").lower()
    except Exception:
        parsed = {"verdict": "fail", "defects": ["reviewer did not return JSON"], "notes": review_text[:1500]}

    final_text = solver_text
    if verdict != "pass":
        repair = _chat(
            gateway_chat_url,
            api_key,
            solver_model,
            _with_system(
                REPAIR_SYSTEM,
                [
                    *user_messages,
                    {"role": "assistant", "content": solver_text[:8000]},
                    {"role": "user", "content": "Defects from independent review:\n" + json.dumps(parsed)[:4000]},
                ],
            ),
            timeout,
        )
        final_text = _content(repair)

    synthesis = (
        final_text
        + "\n\n---\nGX-MAX dual-worker: solver="
        + solver_model
        + " reviewer="
        + reviewer_model
        + " verdict="
        + verdict
    )
    return {
        "id": "gx-max-dual",
        "object": "chat.completion",
        "model": "gx-max",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": synthesis}, "finish_reason": "stop"}],
        "usage": {},
        "gx_max": {
            "mode": "dual-worker",
            "solver": solver_model,
            "reviewer": reviewer_model,
            "verdict": verdict,
        },
    }
