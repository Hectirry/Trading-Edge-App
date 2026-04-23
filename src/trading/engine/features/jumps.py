"""Lee–Mykland jump detection on 1 Hz log returns.

Returns a boolean: any 1-second return in the last ``window_s`` window
whose |Z| exceeds ``z_threshold``, where Z is the return divided by the
bipower-based local vol estimate.
"""

from __future__ import annotations

import math
from collections.abc import Sequence


def lee_mykland_jump_flag(
    spots: Sequence[float], *, window_s: int = 60, z_threshold: float = 3.0
) -> bool:
    if window_s < 4 or len(spots) < window_s + 1:
        return False
    tail = spots[-(window_s + 1):]
    rets: list[float] = []
    for i in range(1, len(tail)):
        if tail[i - 1] <= 0 or tail[i] <= 0:
            rets.append(0.0)
            continue
        rets.append(math.log(tail[i] / tail[i - 1]))
    if len(rets) < 3:
        return False
    # Bipower variation as the robust local vol estimate.
    bipower = 0.0
    for i in range(1, len(rets)):
        bipower += abs(rets[i]) * abs(rets[i - 1])
    k = (math.pi / 2) / max(len(rets) - 1, 1)
    local_var = k * bipower
    if local_var <= 1e-20:
        return False
    sigma = math.sqrt(local_var)
    for r in rets:
        if abs(r) / sigma > z_threshold:
            return True
    return False
