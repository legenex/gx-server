"""A registry-v2 fixture matching ARCHITECTURE-V41 §2, verbatim.

The production registry (legenex/models/registry.json) is owned by the Model
Manager worker; the orchestrator must be tested against the DOCUMENTED
schema, not against whatever the live file happens to contain today. Tests
import `fixture_registry()` (in-memory) or `write_fixture_registry(path)`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURE: dict[str, Any] = {
    "schema": 2,
    "cluster": {"name": "legenex-dual-gx10", "head": "gx10-01", "worker": "gx10-02"},
    "nodes": {
        "gx10-01": {
            "role": "head", "user": "legenex", "lan_ip": "10.60.21.37",
            "tailscale_ip": "100.105.214.61",
            "fabric": {"rail1": "192.168.100.10", "rail2": "192.168.101.10"},
            "hcas": ["rocep1s0f0", "roceP2p1s0f0"], "ssh": "gx10-01",
        },
        "gx10-02": {
            "role": "worker", "user": "legenex-02", "lan_ip": "10.60.21.41",
            "tailscale_ip": "100.73.238.4",
            "fabric": {"rail1": "192.168.100.11", "rail2": "192.168.101.11"},
            "hcas": ["rocep1s0f0", "roceP2p1s0f0"], "ssh": "10.60.21.41",
        },
    },
    "runtimes": {
        "mia-dsv41": {
            "kind": "mia-2x-gb10-exl3", "submodule": "mia-dsv41",
            "commit": "6f7d1590ad49a2b8995188e45d7b9db31e677452",
            "image": "ghcr.io/miaai-lab/deepseek-v4.1-flash-exl3-2x-dgx-sparks:2.9bpw",
            "api": "http://127.0.0.1:8888/v1", "served_model_id": "DeepSeek-v4.1-Flash-EXL3",
            "start": "./start.sh", "stop": "./stop.sh", "status": "./start.sh status",
        },
    },
    "models": {
        "dsv41-flash-exl3-stock": {
            "source": "Mia-AiLab/DeepSeek-V4.1-Flash-EXL3-2.9bpw",
            "revision": "64ba41b6c916a587db06eae2e19b7845f7be6e6b",
            "path": "/srv/models/dsv41/model", "uncensored": False,
            "engram_dir": "/srv/models/dsv41/engram-src", "quant": "exl3-2.9bpw-mul1",
            "vision": True, "tools": True, "max_context": 600000,
        },
        "dsv41-flash-exl3-uncensored": {
            "source": "dealignai/DeepSeek-V4.1-Flash-UNCENSORED-EXL3-2.9bpw",
            "revision": "8a27b35fc5b145fa05ee965c7d7b243b047915f7",
            "path": "/srv/models/dsv41/uncensored", "uncensored": True,
            "engram_dir": "/srv/models/dsv41/engram-src", "quant": "exl3-2.9bpw-mul1",
            "vision": True, "tools": True, "max_context": 262144,
            "serving_notes": {"gpu_mem_util": 0.85, "kv_bytes": 1073741824,
                              "max_num_batched_tokens": 2048,
                              "vllm_sparse_indexer_max_logits_mb": 256},
        },
    },
    "aliases": {
        "gx-max": {"model": "dsv41-flash-exl3-uncensored", "runtime": "mia-dsv41",
                   "mode": "direct", "description": "Explicit DeepSeek V4.1 Flash (uncensored)"},
        "gx-auto": {"model": "dsv41-flash-exl3-uncensored", "runtime": "mia-dsv41",
                    "mode": "auto", "description": "Profile/reasoning auto-selection"},
    },
    "profiles": {
        "fast": {"max_num_seqs": 1, "spec_method": "dspark", "dspark_tokens": 3,
                 "max_model_len": 600000, "reasoning_default": "medium", "target": "single interactive"},
        "balanced": {"max_num_seqs": 2, "spec_method": "dspark", "dspark_tokens": 3,
                     "max_model_len": 600000, "reasoning_default": "medium", "target": "AgentOS default, 2 gens"},
        "swarm": {"max_num_seqs": 4, "spec_method": "none",
                  "max_model_len": 262144, "reasoning_default": "low", "target": "many logical agents, 4 gens"},
        "deep": {"max_num_seqs": 2, "spec_method": "dspark", "dspark_tokens": 3,
                 "max_model_len": 600000, "reasoning_default": "max", "target": "architecture/review, 1-2 streams"},
        "long": {"max_num_seqs": 1, "spec_method": "dspark",
                 "max_model_len": 600000, "reasoning_default": "high", "target": "large repo context, TTFT warn"},
        "custom": {"bounded": True, "max_num_seqs": [1, 4], "max_model_len": [8192, 600000],
                   "reasoning_default": "high", "target": "advanced, bounded"},
    },
    "reasoning": {
        "levels": ["none", "minimal", "low", "medium", "high", "xhigh", "max"],
        "mapping": {"none": {"enable_thinking": False}, "minimal": {"enable_thinking": False},
                    "low": {"reasoning_effort": 50}, "medium": {"reasoning_effort": 62},
                    "high": {"reasoning_effort": 75}, "xhigh": {"reasoning_effort": 90},
                    "max": {"reasoning_effort": 100}},
        "numeric_range": [1, 100],
    },
    "capabilities": {"vision": True, "tools": True, "structured_output": True, "reasoning": True},
}


def fixture_registry_dict() -> dict[str, Any]:
    """A deep copy of the fixture (callers may mutate freely)."""
    return json.loads(json.dumps(FIXTURE))


def write_fixture_registry(path: "str | Path") -> Path:
    """Write the fixture registry to `path` and return it."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(fixture_registry_dict(), indent=1), encoding="utf-8")
    return p


def load_fixture_registry():
    """The fixture parsed through the real loader (validated like production)."""
    import sys
    from gx_orchestrator.profiles import parse_registry
    return parse_registry(fixture_registry_dict())
