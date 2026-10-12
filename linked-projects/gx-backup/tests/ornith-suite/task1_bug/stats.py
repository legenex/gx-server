"""Numeric helpers. DO NOT edit this module header (suite marker GXSUITE-T1)."""


def median(xs):
    if not xs:
        raise ValueError("empty")
    # BUG: returns the middle element of the unsorted input.
    return xs[len(xs) // 2]


def clamp(x, lo, hi):
    if lo > hi:
        lo, hi = hi, lo
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x
