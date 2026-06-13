from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def ensure_crackersdk_importable() -> None:
    if importlib.util.find_spec("crackersdk") is not None:
        return

    repo_root = Path(__file__).resolve().parents[2]
    submodule_src = repo_root / "external" / "cracker-sdk" / "src"
    package_init = submodule_src / "crackersdk" / "__init__.py"
    if not package_init.is_file():
        return

    value = str(submodule_src)
    if value not in sys.path:
        sys.path.insert(0, value)


__all__ = ["ensure_crackersdk_importable"]
