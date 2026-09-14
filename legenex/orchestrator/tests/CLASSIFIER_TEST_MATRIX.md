# classifier.py / route() test matrix

Scratch/companion test artifact for `tests/test_classifier.py`. Not one of the
lead's top-level docs (CURRENT_STATE.md, ARCHITECTURE.md, etc.) — this is a
working reference for the routing test suite only. Ownership: whoever touches
`classifier.py`/`test_classifier.py` should keep this in sync with them, same
as any other test file comment.

Baseline before this pass: 31 tests total across the orchestrator (21 in
`test_classifier.py`, 10 in `test_lifecycle.py`), all passing. After this
pass: 61 total (51 + 10), all passing. Two real bugs found and fixed in
`classifier.py` (see "Bugs found" below); no changes to `tiers.py` (read-only,
no bug found there) or `server.py`/`lifecycle.py` (out of scope for this pass).

## Rule coverage (ARCHITECTURE.md section 6)

| Rule | Existing test (baseline) | New boundary/edge tests added |
|---|---|---|
| context > `MAX_SINGLE_NODE_CONTEXT` → gx-max | `TestTierSelection.test_huge_context_forces_max` (way over) | `TestContextBoundary.test_context_exactly_at_threshold_does_not_force_max` (== threshold, must NOT be max), `test_context_one_token_over_threshold_forces_max` (threshold+1, must be max), `test_large_context_threshold_matches_derived_constant` |
| `hard` score ≥ `HARD_SCORE_MAX` (3) → gx-max | `test_explicit_extreme_to_max` (score 11, well over) | `TestHardScoreBoundary.test_hard_score_exactly_at_threshold_triggers_max` (score == 3 exactly, single pattern), `test_hard_score_zero_never_reaches_max_alone` |
| total complexity ≥ `COMPLEXITY_REASON` (4) → gx-reason | `test_hard_reasoning_to_reason_not_max` (score 7-ish) | `TestComplexityBoundaries.test_complexity_exactly_at_reason_threshold` (score == 4 exactly), `test_complexity_one_below_reason_threshold_stays_fast` (score == 3, must stay gx-fast) |
| total complexity ≥ `COMPLEXITY_FAST` (1), or tools present → gx-fast | `test_tool_floor_raises_to_fast`, `test_dispatch_to_mini` | `TestComplexityBoundaries.test_complexity_exactly_at_fast_threshold` (score == 1 exactly), `test_complexity_zero_stays_mini` (score == 0, must stay gx-mini) |
| otherwise → gx-mini | `test_trivial_to_mini` | covered by the score==0 boundary test above |
| vision is a capability, not a tier | `TestVisionRouting` (2 tests) | `TestMixedSignals.test_image_alone_on_huge_context_still_respects_context_constraint` — a case the *existing* tests never reached: image + context so large that **no** vision tier can hold it |
| gx-auto may downgrade on unavailability; must log `downgraded_from` | `TestAvailabilityFallback` (4 tests) | `TestDowngradeLogging.test_multi_hop_fallback_records_original_tier_not_intermediate_hop` (2 tiers down, unskipped hop must not appear as the recorded origin), `test_downgraded_from_is_none_when_nothing_changes`, `test_downgrade_reason_is_present_in_log_dict` (checks the actual `as_log_dict()` output, not just the dataclass field) |
| direct gx-max / gx-reason requests bypass gx-auto entirely | N/A — enforced in `server.py`, not `classifier.py` | Not testable from this file; **verified by reading code**, see "Out-of-file findings" below. No test added (would require mocking `server.py`, out of scope for this pass). |

## New edge-case classes

| Test class | What it probes |
|---|---|
| `TestComplexityBoundaries` | complexity score exactly at 0, 1, 3, 4 |
| `TestContextBoundary` | context exactly at / one over `MAX_SINGLE_NODE_CONTEXT` |
| `TestHardScoreBoundary` | hard score exactly at `HARD_SCORE_MAX`; confirms 1/2 are unreachable (all hard patterns weigh 3 or 4) so the only real boundary is 0-vs-3 |
| `TestHardCategoryFalsePositives` | adversarial keyword confusion: "extremely simple" (literal word "extreme" is NOT a pattern — category name ≠ keyword), bare "exhaustive"/"comprehensive" without a qualifying noun, reasoning-only words (debug/derive/prove/refactor) that must never bleed into the `hard` category no matter how many stack up |
| `TestMixedSignals` | image + tools + explicit-extreme text together; image + tools + trivial text (tool floor still applies); image on a context so large no vision tier can hold it |
| `TestVeryLongPrompts` | trivial-scored request nudged off gx-mini purely by context size; a very long prompt safely under the max-context threshold |
| `TestDegenerateInput` | `{}`, `messages: None`, system-only messages, `content: None`, junk list parts, uppercase role, empty string |
| `TestDowngradeLogging` | `downgraded_from` correctness across multi-hop fallback and the JSON log dict |

## Bugs found and fixed (in `classifier.py`, `_HARD_PATTERNS` and `route()`)

### Bug 1 — bare "exhaustive" reached gx-max on a single word

`_HARD_PATTERNS` had:

```python
(r"\bexhaustiv|\bcomprehensive\b.*\b(analysis|review|audit)\b", 3),
```

Regex alternation (`|`) is not scoped by the trailing `.*\b(analysis|review|
audit)\b` — that qualifier only applied to the `comprehensive` branch. Any
message containing "exhaustive" (e.g. *"Please give an exhaustive list of
HTTP status codes"*) scored `hard_score = 3 = HARD_SCORE_MAX` from that one
word alone and routed straight to **gx-max**, which evicts every model on
both nodes (L-6). This is a direct violation of D-005's whole point — not
"accumulation" but a single ordinary word reaching the threshold outright.

Fix: require the qualifying noun for both alternatives —
`r"\b(exhaustiv\w*|comprehensive)\b.*\b(analysis|review|audit)\b"`. Verified
the fix still matches genuine cases ("exhaustive analysis", "comprehensive
audit") and no longer matches the bare word. See
`TestHardCategoryFalsePositives` (4 tests) for the regression pins.

### Bug 2 — vision override ignored context fit

In `route()`, once a request needed a tier with vision and the chosen tier
lacked it, the code picked "the most capable vision tier at or below the
current cost_rank" — with no check that the tier's `max_context` could
actually hold the request. A ~292k-token prompt (over
`MAX_SINGLE_NODE_CONTEXT`, so `_base_tier` correctly picked gx-max) plus one
image got silently re-routed to **gx-reason**, whose served context
(131,072) is well under half of what the request needed — the exact failure
mode `estimate_tokens()`'s pessimistic rounding exists to prevent (see the
module's own top-of-file comment: "we never route a prompt to a tier whose
context cannot hold it").

Fix: filter candidate vision tiers to `TIERS[t].max_context >=
f.total_context_needed` before picking by cost; if none qualify, stay on the
context-safe tier `_base_tier` already chose (logged, not silent). Verified
against `TestMixedSignals.test_image_alone_on_huge_context_still_respects_context_constraint`
and the existing `TestVisionRouting` / `TestAvailabilityFallback` vision
tests, which are all small-context and unaffected by the fix.

## Out-of-file findings (confirmed by reading, not modified — outside this
task's file scope)

* `server.py::Handler._handle_inference` accepts only `gx-auto` and `gx-max`
  at the orchestrator; every other alias (including `gx-reason`, `gx-fast`,
  `gx-mini`) is rejected with a 400 telling the caller to hit LiteLLM
  directly. So a direct `gx-reason` or `gx-fast` request **never reaches
  `classifier.py`/`route()` at all** — confirmed also from
  `legenex/gateway/litellm/config.yaml`, where those aliases point straight
  at their llama-swap upstreams.
* `server.py::Handler._serve_gx_max` (the direct-gx-max path) calls
  `lifecycle.acquire()` and, on failure, returns a 503
  (`gx_max_unavailable`) — it never calls `route()` and never substitutes a
  smaller tier. Confirmed live in `test_failed_acquisition_raises_and_never_downgrades`
  in `test_lifecycle.py`.
* `legenex/gateway/litellm/config.yaml`'s `litellm_settings` sets
  `num_retries: 0`, `fallbacks: []`, `context_window_fallbacks: []`,
  `content_policy_fallbacks: []` globally, with an explicit comment that this
  is intentional for every alias, not just gx-max. There is **no config knob**
  anywhere in this stack that allows a direct `gx-reason` request to be
  silently served by `gx-fast` (or anything else). This matches the documented
  intent and needs no fix.
