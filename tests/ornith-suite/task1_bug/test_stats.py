import pytest
from stats import clamp, median


def test_median_odd_unsorted():
    assert median([9, 1, 5]) == 5


def test_median_even_average():
    assert median([4, 1, 3, 2]) == 2.5


def test_median_empty():
    with pytest.raises(ValueError):
        median([])


def test_clamp_ok():
    assert clamp(5, 0, 10) == 5
    assert clamp(-1, 0, 10) == 0
    assert clamp(99, 0, 10) == 10
