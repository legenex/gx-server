"""GXSUITE-T6 incomplete calculator."""


def add(a, b):
    return a + b


def sub(a, b):
    return a - b


def mul(a, b):
    return a * b


def div(a, b):
    # BUG: integer division and no zero check
    return a // b
