from __future__ import annotations


def weighted_sum(values: list[int], weight: int) -> int:
    return sum(v * weight for v in values)
