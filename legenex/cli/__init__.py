"""gx CLI package (legenex.cli).

Exposes main() so the CLI runs as:
    python3 -m legenex.cli          (from the repo root)
    python3 -m gx_cli               (from legenex/cli/, or with it on PYTHONPATH)
    ~/.local/bin/gx                 (symlink created by the install step)
"""

from .gx import main

__all__ = ["main"]
