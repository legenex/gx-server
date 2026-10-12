import pytest
from ops import add, div, mean, mul, power, sub


def test_arithmetic():
    assert add(2, 3) == 5
    assert sub(10, 4) == 6
    assert mul(3, 5) == 15


def test_div_true_division():
    assert div(5, 2) == 2.5
    with pytest.raises(ZeroDivisionError):
        div(1, 0)


def test_power():
    assert power(2, 8) == 256
    assert power(9, 0.5) == 3.0


def test_mean():
    assert mean([1, 2, 3, 4]) == 2.5
    with pytest.raises(ValueError):
        mean([])
