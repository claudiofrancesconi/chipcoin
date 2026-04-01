"""Time helpers kept outside consensus-critical logic."""

from __future__ import annotations

import time


def unix_time() -> int:
    """Return the current UNIX timestamp as an integer."""

    return int(time.time())
