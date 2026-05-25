from __future__ import annotations

from app.core.mathops import weighted_sum


def test_weighted_sum() -> None:
    assert weighted_sum([1, 2, 3], 2) == 12
