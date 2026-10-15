#!/usr/bin/env python3
"""Fingerprint the NCCL a Python environment will load, and compare two fingerprints.

Run inside the image build before and after the FlashInfer/b12x installs
(security review condition 3): FlashInfer's source build can upgrade
nvidia-nccl-cu13 behind torch's back, and a different libnccl changes the
fabric code path. The build fails when the fingerprints differ.

    nccl_fingerprint.py                 # print JSON
    nccl_fingerprint.py --compare A B   # exit 1 if A != B
"""
from __future__ import annotations

import hashlib
import json
import sys
from importlib import metadata
from pathlib import Path

_ROOTS = ("/usr/local/lib/python3.12/dist-packages", "/usr/lib/python3/dist-packages",
          "/usr/lib/aarch64-linux-gnu", "/usr/local/cuda/lib64", "/usr/local/lib")


def fingerprint() -> dict:
    dists = {}
    for dist in metadata.distributions():
        name = (dist.metadata.get("Name") or "").lower()
        if "nccl" in name:
            dists[name] = dist.version
    libs = {}
    for root in _ROOTS:
        base = Path(root)
        if not base.is_dir():
            continue
        for lib in sorted(base.rglob("libnccl*.so*")):
            if lib.is_file() and not lib.is_symlink():
                libs[str(lib)] = hashlib.sha256(lib.read_bytes()).hexdigest()
    return {"distributions": dists, "libraries": libs}


def main(argv: list[str]) -> int:
    if argv[:1] == ["--compare"] and len(argv) == 3:
        a = json.loads(Path(argv[1]).read_text())
        b = json.loads(Path(argv[2]).read_text())
        if a != b:
            print("NCCL CHANGED during the build:", json.dumps({"before": a, "after": b}, indent=1))
            return 1
        print(f"NCCL unchanged: {json.dumps(a['distributions'])}, {len(a['libraries'])} libraries")
        return 0
    print(json.dumps(fingerprint(), indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
