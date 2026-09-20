# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
#
# GX_CLUSTER_DFLASH2_OVERLAY: DFlash2 worker adapted for
# jstarkg/vllm-gb10-flashnext:0.28-sm121-r6 (no gumbel_noised_argmax helper).

from typing import Any

import torch

from vllm.config import VllmConfig
from vllm.config.compilation import CUDAGraphMode
from vllm.v1.worker.gpu.spec_decode.dflash.speculator import DFlashSpeculator


class DFlash2Speculator(DFlashSpeculator):
    _speculator_name = "DFlash2"

    def __init__(self, vllm_config: VllmConfig, device: torch.device):
        super().__init__(vllm_config, device)
        draft_config = self.draft_model_config.hf_config.dflash_config
        self.selector_top_k = int(draft_config["selector_top_k"])
        self.enable_adaptive_verification = bool(
            getattr(self.speculative_config, "enable_adaptive_verification", False)
        )
        self._anchor_indices = (
            torch.arange(self.max_num_reqs, dtype=torch.int64, device=device)
            * self.num_query_per_req
        )
        self._selector_scores = torch.empty(
            self.max_num_reqs,
            self.num_speculative_steps,
            self.selector_top_k,
            dtype=torch.float32,
            device=device,
        )
        self._cached_candidate_ids = torch.zeros(
            self.max_num_reqs,
            self.num_speculative_steps,
            self.selector_top_k,
            dtype=torch.int64,
            device=device,
        )

    def draft_logits_spec(self, vllm_config: VllmConfig) -> tuple[torch.dtype, float]:
        return torch.float32, -float("inf")

    def _sample_path(
        self,
        candidate_ids: torch.Tensor,
        scores: torch.Tensor,
        num_reqs: int,
    ) -> None:
        """Walk the candidate lattice with Gumbel-max (torch path).

        scores: [num_reqs, steps, top_k_prev, top_k_next] from CandidateSelector
        candidate_ids: [num_reqs, steps, top_k]
        """
        top_k = self.selector_top_k
        steps = self.num_speculative_steps
        device = candidate_ids.device

        # scores from selector is [B, L, K, K] after einsum blpc
        # Upstream stores realized scores into _selector_scores as [B, L, K]
        # for the chosen previous index's outgoing edges.
        prev_idx = torch.zeros(num_reqs, dtype=torch.int64, device=device)
        out_tokens = torch.empty(
            num_reqs, steps, dtype=torch.int64, device=device
        )

        for step in range(steps):
            # Edge scores for current previous candidate index.
            # scores shape: [B, L, K_prev, K_next]
            if scores.dim() == 4:
                step_scores = scores[:num_reqs, step]  # [B, K_prev, K_next]
                gather_index = prev_idx.view(num_reqs, 1, 1).expand(
                    num_reqs, 1, top_k
                )
                edge = step_scores.gather(1, gather_index).squeeze(1)  # [B, K]
            else:
                edge = scores[:num_reqs, step]

            self._selector_scores[:num_reqs, step].copy_(edge.to(torch.float32))

            # Temperature / Gumbel from per-request buffers.
            mapping = self.sample_idx_mapping[
                step : num_reqs * steps : steps
            ]
            # mapping is per sample row; rebuild per-req mapping from first step rows
            req_map = self.sample_idx_mapping[::steps][:num_reqs]
            valid = req_map >= 0
            temp = torch.zeros(num_reqs, device=device, dtype=torch.float32)
            seeds = torch.zeros(num_reqs, device=device, dtype=torch.int64)
            temp[valid] = self.temperature[req_map[valid]].to(torch.float32)
            seeds[valid] = self.seeds[req_map[valid]]

            logits = edge.to(torch.float32)
            # sample_pos is predicted token position P; key Gumbel by P-1
            sample_rows = torch.arange(num_reqs, device=device) * steps + step
            pos = self.sample_pos[sample_rows] - 1

            use_noise = (temp != 0) & (self.draft_logits is not None)
            # Always allow greedy when temp==0; when probabilistic, add Gumbel.
            if bool((temp != 0).any().item()) and self.draft_logits is not None:
                # Gumbel-max noise keyed by (seed, pos, candidate)
                # u ~ Uniform(0,1); g = -log(-log(u))
                cand = torch.arange(top_k, device=device).view(1, top_k)
                # simple hash mix for reproducibility without triton helper
                mix = (
                    seeds.view(num_reqs, 1).to(torch.int64)
                    ^ (pos.view(num_reqs, 1).to(torch.int64) * 0x9E3779B97F4A7C15)
                    ^ (cand * 0xBF58476D1CE4E5B9)
                )
                # Convert mix to (0,1) via hashing into float bits
                u = ((mix.to(torch.int64) & 0xFFFFFF).to(torch.float32) + 1.0) / float(
                    0x1000000
                )
                u = u.clamp(min=1e-6, max=1.0 - 1e-6)
                gumbel = -torch.log(-torch.log(u))
                scaled = torch.where(
                    (temp > 0).view(num_reqs, 1),
                    logits / temp.clamp(min=1e-6).view(num_reqs, 1) + gumbel,
                    logits,
                )
            else:
                scaled = logits

            scaled = torch.where(
                valid.view(num_reqs, 1), scaled, torch.full_like(scaled, float("-inf"))
            )
            idx = scaled.argmax(dim=-1)
            prev_idx = idx
            out_tokens[:num_reqs, step] = candidate_ids[:num_reqs, step].gather(
                1, idx.view(num_reqs, 1)
            ).squeeze(1)

        self.draft_tokens[:num_reqs].copy_(out_tokens)

    def _cache_draft_logits(self, candidate_ids: torch.Tensor, num_sample: int) -> None:
        draft_logits = self.draft_logits
        if draft_logits is None:
            return
        num_reqs = num_sample // self.num_speculative_steps
        top_k = self.selector_top_k
        for flat in range(num_sample):
            req_state = int(self.sample_idx_mapping[flat].item())
            if req_state < 0:
                continue
            step = flat % self.num_speculative_steps
            old_ids = self._cached_candidate_ids[req_state, step]
            draft_logits[req_state, step, old_ids] = float("-inf")
            new_ids = candidate_ids.view(num_reqs, self.num_speculative_steps, top_k)[
                req_state if req_state < num_reqs else 0, step
            ]
            # Map via sample_idx_mapping row -> request index in this batch
            # Prefer writing by req_state directly (matches upstream cache layout).
            scores = self._selector_scores[req_state, step]
            draft_logits[req_state, step, new_ids] = scores.to(draft_logits.dtype)
            self._cached_candidate_ids[req_state, step].copy_(new_ids)

    def _generate_draft(
        self,
        num_reqs: int,
        num_tokens_padded: int,
        attn_metadata: dict[str, Any] | None,
        slot_mappings: dict[str, torch.Tensor] | None,
        num_tokens_across_dp: torch.Tensor | None,
        cudagraph_runtime_mode: CUDAGraphMode = CUDAGraphMode.NONE,
    ) -> None:
        last_hidden_states = self._run_model(
            num_tokens_padded,
            attn_metadata,
            slot_mappings,
            num_tokens_across_dp,
            cudagraph_runtime_mode,
        )
        num_sample = num_reqs * self.num_speculative_steps
        hidden_states = last_hidden_states[self.sample_indices[:num_sample]].view(
            num_reqs, self.num_speculative_steps, -1
        )
        candidate_ids, unary_logits = self.model.compute_candidates(
            hidden_states.flatten(0, 1)
        )
        candidate_ids = candidate_ids.view(
            num_reqs, self.num_speculative_steps, self.selector_top_k
        )
        unary_logits = unary_logits.view_as(candidate_ids)
        anchor_token_ids = self.input_buffers.input_ids[self._anchor_indices[:num_reqs]]
        scores = self.model.model.candidate_selector(
            candidate_ids,
            unary_logits,
            hidden_states,
            anchor_token_ids,
        )
        self._sample_path(candidate_ids, scores, num_reqs)
        if self.draft_logits is not None:
            self._cache_draft_logits(candidate_ids, num_sample)
