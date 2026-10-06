"""Fleet wide over temperature and load statistics.

The helpers here are thin projections of the per-cable statistics, never an
independent calculation: each cable is classified from the same series that
feeds its history window.  Console code reads the shared
:class:`~tunnelnet.cable.stats.StatsRun` instead (``run.danger_count`` etc.).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .registry import CableRegistry


def danger_count(registry: "CableRegistry", threshold: float) -> int:
    return sum(
        1
        for cable_id in registry.cable_ids()
        if registry.stats(cable_id, threshold).status == "over"
    )


def healthy_count(registry: "CableRegistry", threshold: float) -> int:
    return sum(
        1
        for cable_id in registry.cable_ids()
        if registry.stats(cable_id, threshold).status == "normal"
    )


def load_factor(registry: "CableRegistry") -> float:
    currents = [
        current
        for cable_id in registry.cable_ids()
        if (current := registry.current(cable_id)) is not None
    ]
    return sum(currents) / len(currents) if currents else 0.0
