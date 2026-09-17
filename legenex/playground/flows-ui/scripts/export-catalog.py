"""Writes the backend node catalogue as the flows-ui test fixture (or checks it).

    python3 scripts/export-catalog.py          # rewrite test/fixtures/catalog.json
    python3 scripts/export-catalog.py --check  # exit 1 if the fixture is stale
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "control-ui"))

from gx_control_ui.flows.catalog import catalog_public  # noqa: E402

target = HERE.parent / "test" / "fixtures" / "catalog.json"
text = json.dumps(catalog_public(), indent=1, sort_keys=True) + "\n"
if "--check" in sys.argv:
    if not target.exists() or target.read_text(encoding="utf-8") != text:
        print("flows-ui/test/fixtures/catalog.json is stale: run python3 scripts/export-catalog.py", file=sys.stderr)
        sys.exit(1)
    print("catalogue fixture up to date")
else:
    target.write_text(text, encoding="utf-8")
    print(f"wrote {target}")
