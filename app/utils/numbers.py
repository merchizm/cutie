from __future__ import annotations


def even_floor(value: int) -> int:
    value = max(value, 0)
    return value if value % 2 == 0 else value - 1
