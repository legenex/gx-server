"""Tiny in-memory item API. GXSUITE-T3. Do not rename list_items."""

from typing import Any

_ITEMS: list[dict[str, Any]] = []
_NEXT = 1


def reset() -> None:
    global _NEXT
    _ITEMS.clear()
    _NEXT = 1


def list_items() -> list[dict[str, Any]]:
    return list(_ITEMS)


def get_item(item_id: int) -> dict[str, Any] | None:
    for item in _ITEMS:
        if item["id"] == item_id:
            return item
    return None


# BUG/MISSING: create_item is not implemented. Tests expect:
# create_item(name: str, qty: int) -> dict
# - name non-empty str, qty int >= 1
# - ValueError otherwise
# - assigns incremental integer id starting at 1
# - stores {"id", "name", "qty"}
