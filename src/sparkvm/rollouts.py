"""Compatibility wrapper for rollout APIs."""

import sys as _sys

from .api import rollouts as _rollouts
from .api.rollouts import *  # noqa: F401,F403

_sys.modules[__name__] = _rollouts
