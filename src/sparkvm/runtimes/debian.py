"""Guest init script template injected into dockified runtime images."""

from __future__ import annotations

from sparkvm.constants import DEBIAN_MINBASE_IMAGE_ID, INIT_TEMPLATE, SPARKVM_INIT_TEMPLATE

__all__ = ["SPARKVM_INIT_TEMPLATE", "INIT_TEMPLATE", "DEBIAN_MINBASE_IMAGE_ID"]
