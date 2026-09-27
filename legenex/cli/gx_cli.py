"""`python3 -m gx_cli` entry point.

A thin wrapper so the CLI is runnable as a top-level module when
legenex/cli/ is on PYTHONPATH (the install step's ~/.local/bin/gx symlink
covers the normal case; this module covers the no-install case). Keep this
file import-light: it only finds gx.py and delegates.
"""

from __future__ import annotations

import sys
from pathlib import Path

# When this module is imported as a top-level module (python3 -m gx_cli from
# legenex/cli, or with that dir on PYTHONPATH), gx.py sits next to it and is
# importable directly. When imported as part of the legenex.cli package, use
# the relative import instead.
try:
    from .gx import main  # type: ignore[import-not-found]  # package context
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from gx import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
