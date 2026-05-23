from __future__ import annotations

import json
from pathlib import Path

from app.core.mathops import weighted_sum


def main() -> None:
    payload = json.loads(Path("data/input.json").read_text(encoding="utf-8"))
    values = [int(x) for x in payload["values"]]
    weight = int(payload["weight"])
    total = weighted_sum(values, weight)
    print("computed_total=", total)


if __name__ == "__main__":
    main()
