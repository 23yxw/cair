"""Slice selection strategies for image series.

Pure functions over slice counts so they are trivially testable; the tool layer
maps the returned indices back to Image records.
"""

from __future__ import annotations

from typing import List, Optional

SMALL_SERIES_MAX = 6  # series with <= this many images are sent in full


def uniform_indices(count: int, n: int) -> List[int]:
    """n indices spread evenly over [0, count), always including first and last when n >= 2."""
    if count <= 0:
        return []
    if n >= count:
        return list(range(count))
    if n == 1:
        return [count // 2]
    step = (count - 1) / (n - 1)
    idx = [round(i * step) for i in range(n)]
    # de-duplicate while preserving order (rounding can collide for tiny counts)
    seen, out = set(), []
    for i in idx:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def neighbor_indices(count: int, center: int, k: int) -> List[int]:
    """center +/- k, clipped to the valid range."""
    if count <= 0:
        return []
    center = max(0, min(count - 1, center))
    return list(range(max(0, center - k), min(count, center + k + 1)))


def anchor_indices(count: int, anchor: Optional[int]) -> List[int]:
    if count <= 0:
        return []
    if anchor is None or not 0 <= anchor < count:
        anchor = count // 2
    return [anchor]


def select(strategy: str, count: int, *, n: int = 8, anchor: Optional[int] = None,
           center: Optional[int] = None, k: int = 2) -> List[int]:
    """Dispatch. Small series ignore the strategy and return everything."""
    if count <= SMALL_SERIES_MAX:
        return list(range(count))
    if strategy == "anchors":
        return anchor_indices(count, anchor)
    if strategy == "uniform":
        return uniform_indices(count, n)
    if strategy == "neighbors":
        c = center if center is not None else (anchor if anchor is not None else count // 2)
        return neighbor_indices(count, c, k)
    raise ValueError(f"unknown strategy {strategy!r}")
