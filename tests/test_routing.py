#!/usr/bin/env python3
"""Classifier must send coding to gx-code and never silent-downgrade to mini."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path.home() / "Documents/Projects/Server/gx-cluster/legenex/orchestrator"))
from gx_orchestrator.classifier import route
from gx_orchestrator.tiers import Tier


def test_hi_is_mini():
    assert route({"messages": [{"role": "user", "content": "hi"}]}).tier is Tier.MINI


def test_coding_is_code():
    d = route({"messages": [{"role": "user", "content": "implement login API in python"}]})
    assert d.tier is Tier.CODE, d


def test_busy_code_does_not_fall_to_mini():
    d = route(
        {"messages": [{"role": "user", "content": "fix the bug in auth.py"}]},
        available={Tier.CODE: False, Tier.MINI: True, Tier.MAX: False},
        busy={Tier.CODE: True},
    )
    assert d.tier is Tier.CODE
    assert any("do not downgrade" in r for r in d.reasons)


if __name__ == "__main__":
    test_hi_is_mini()
    test_coding_is_code()
    test_busy_code_does_not_fall_to_mini()
    print("OK")
