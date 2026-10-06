"""The single cable statistics calibre shared by reports, lists and exports.

Every consumer — the stats endpoint, the health counters, the dashboard,
telemetry and the CSV exports — must derive its numbers through
:func:`cable_statistics` (or the registry helpers it uses) so they can never
disagree.  Each computed result is also persisted and audited by the
console, so a statistic can always be traced back to the sample series it
came from.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .model import OVER_TEMPERATURE, status_for
from .series import GAP_POLICY, ORDER_POLICY, utc_now

if TYPE_CHECKING:  # pragma: no cover - imported only for type checkers
    from .registry import CableRegistry


@dataclass(frozen=True)
class CableStats:
    """One consistent read of a cable series at a point in time."""

    cable_id: str
    at: str
    threshold: float
    sample_count: int
    current: float
    status: str
    average: float
    maximum: float
    minimum: float
    over: int
    gap_policy: str
    order_policy: str

    def as_dict(self) -> dict[str, object]:
        return {
            "cable_id": self.cable_id,
            "at": self.at,
            "threshold": self.threshold,
            "sample_count": self.sample_count,
            "current": self.current,
            "status": self.status,
            "average": self.average,
            "max": self.maximum,
            "min": self.minimum,
            "over": self.over,
            "gap_policy": self.gap_policy,
            "order_policy": self.order_policy,
        }


def cable_statistics(
    registry: "CableRegistry",
    cable_id: str,
    threshold: float = OVER_TEMPERATURE,
    at: str = "",
) -> CableStats:
    """Compute every cable number from the one persisted sample series."""

    values = registry.history(cable_id)
    state = registry.get(cable_id)
    current = state.temp if state is not None else 0.0
    return CableStats(
        cable_id=cable_id,
        at=at or utc_now(),
        threshold=threshold,
        sample_count=len(values),
        current=current,
        status=status_for(current),
        average=sum(values) / len(values) if values else 0.0,
        maximum=max(values) if values else 0.0,
        minimum=min(values) if values else 0.0,
        over=sum(1 for value in values if value >= threshold),
        gap_policy=GAP_POLICY,
        order_policy=ORDER_POLICY,
    )
