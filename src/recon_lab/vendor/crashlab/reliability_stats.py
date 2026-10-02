"""Dependency-free Wilson score interval for binomial proportions."""

from __future__ import annotations

import math

WILSON_Z = 1.959963984540054


def wilson_interval(successes: int, trials: int, z: float = WILSON_Z) -> tuple[float, float]:
    if trials <= 0:
        raise ValueError("trials must be positive")
    if successes < 0 or successes > trials:
        raise ValueError("successes must be between zero and trials")
    p = successes / trials
    z2 = z * z
    denominator = 1.0 + z2 / trials
    center = (p + z2 / (2.0 * trials)) / denominator
    margin = (
        z
        * math.sqrt((p * (1.0 - p) / trials) + (z2 / (4.0 * trials * trials)))
        / denominator
    )
    return max(0.0, center - margin), min(1.0, center + margin)
