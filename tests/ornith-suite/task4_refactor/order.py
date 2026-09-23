"""GXSUITE-T4 order pricing. Tax math is duplicated with user.py."""

TAX_RATE = 0.15


def apply_tax(amount: float) -> float:
    return round(float(amount) * (1 + TAX_RATE), 2)


def order_total(lines: list[float]) -> float:
    return apply_tax(sum(lines))
