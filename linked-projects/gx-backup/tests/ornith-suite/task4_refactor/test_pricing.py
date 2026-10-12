from pathlib import Path

import order
import user

ROOT = Path(__file__).resolve().parent


def test_user_total_unchanged():
    assert user.user_total(100) == 115.0
    assert user.user_total(100, discount=20) == 92.0


def test_order_total_unchanged():
    assert order.order_total([10, 10, 10]) == 34.5


def test_shared_tax_module():
    tax = ROOT / "tax.py"
    assert tax.is_file(), "expected tax.py as the single source of tax math"
    src_user = (ROOT / "user.py").read_text()
    src_order = (ROOT / "order.py").read_text()
    assert "TAX_RATE" not in src_user
    assert "TAX_RATE" not in src_order
    assert "from tax import" in src_user or "import tax" in src_user
    assert "from tax import" in src_order or "import tax" in src_order
