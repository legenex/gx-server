#!/usr/bin/env python3
"""Make mamba-ssm / causal-conv1d buildable and fast on GB10 + torch 2.14.

Run inside the gx-call engine image's BUILDER stage, on the unpacked sdists,
before ``pip wheel``. Both packages hardcode two things in their ``setup.py``
that no environment variable can override (see the long comment in the
Dockerfile for the evidence):

1. ``-std=c++17`` in both ``extra_compile_args["cxx"]`` and ``["nvcc"]``.
   torch 2.14's headers require C++20, so mamba-ssm dies with
   ``ATen/ATen.h:5: #error C++20 or later compatible compiler is required``.
2. A hand-built nine-architecture ``-gencode`` list (sm_75 … sm_121) that is
   passed as ``... + cc_flag``. Because the caller already supplies
   ``-gencode``, torch's own ``_get_cuda_arch_flags`` stays out of the way and
   ``TORCH_CUDA_ARCH_LIST`` has no effect. GB10 is sm_121 and runs sm_120
   cubins (legenex/media/README.md), so eight of the nine are wasted time.

The patch asserts what it expects to find. If a future version of either
package changes these lines, the build fails loudly here instead of silently
compiling the wrong thing.
"""

from __future__ import annotations

import pathlib
import sys

CXX17 = '"-std=c++17"'
CXX20 = '"-std=c++20"'
#: how both packages splice their architecture list into the compiler args
CC_FLAG = "+ cc_flag"
#: GB10: sm_121, and sm_120 cubins execute on it (same Blackwell major)
GB10_ARCH = '+ ["-gencode", "arch=compute_120,code=sm_120"]'
MIN_STD = 2   # one in "cxx", one in "nvcc" (the CUDA branch alone)
MIN_ARCH = 1  # the CUDA branch (the HIP branch has one too and is unused here)


def patch(package_dir: str) -> None:
    setup = pathlib.Path(package_dir) / "setup.py"
    source = setup.read_text(encoding="utf-8")
    n_std = source.count(CXX17)
    n_arch = source.count(CC_FLAG)
    if n_std < MIN_STD or n_arch < MIN_ARCH:
        raise SystemExit(
            f"{setup}: expected at least {MIN_STD}x {CXX17} and {MIN_ARCH}x '{CC_FLAG}', "
            f"found {n_std} and {n_arch}. Upstream changed: re-check this patch against "
            "torch's C++20 requirement and the GB10 architecture before building.")
    patched = source.replace(CXX17, CXX20).replace(CC_FLAG, GB10_ARCH)
    setup.write_text(patched, encoding="utf-8")
    print(f"patched {setup}: {n_std}x -std=c++17 -> c++20, {n_arch}x cc_flag -> sm_120 only", flush=True)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    for package_dir in argv:
        patch(package_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
