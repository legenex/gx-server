"""GXSUITE-T4 user pricing. Tax math is duplicated with order.py."""

TAX_RATE = 0.15


def apply_tax(amount: float) -> float:
    return round(float(amount) * (1 + TAX_RATE), 2)


def user_total(subtotal: float, discount: float = 0.0) -> float:
    return apply_tax(max(subtotal - discount, 0.0))
