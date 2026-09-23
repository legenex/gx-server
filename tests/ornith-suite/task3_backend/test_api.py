import pytest
from api import create_item, get_item, list_items, reset


def setup_function():
    reset()


def test_create_and_list():
    a = create_item("widget", 3)
    b = create_item("gadget", 1)
    assert a["id"] == 1
    assert b["id"] == 2
    assert a["name"] == "widget" and a["qty"] == 3
    assert list_items() == [a, b]
    assert get_item(1) == a


def test_rejects_bad_name():
    with pytest.raises(ValueError):
        create_item("", 1)
    with pytest.raises(ValueError):
        create_item("  ", 1)


def test_rejects_bad_qty():
    with pytest.raises(ValueError):
        create_item("x", 0)
    with pytest.raises(ValueError):
        create_item("x", -2)
