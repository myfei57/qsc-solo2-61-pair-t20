"""Cable temperature statistics derived from one sampling series.

All projections share one derivation.  Given the samples of a cable, in sample
time order:

* the current state is the last *valid* sample (gaps skipped);
* the history window, the average, the extrema and the over temperature count
  are the same windowed view of the valid samples.

Fleet counters (danger/healthy/load factor) are simply the per-cable results
rolled up, so a report that shows a history average and a list that shows the
current temperature, together with the over temperature count, always agree.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import status_for
from .series import QUALITY_GAP, Sample, order_samples


def windowed(
    samples: list[Sample],
    start: str | None = None,
    end: str | None = None,
) -> list[Sample]:
    """Ordered samples whose sample time falls inside the inclusive window."""

    ordered = order_samples(samples)
    if start is not None:
        ordered = [sample for sample in ordered if sample.at >= start]
    if end is not None:
        ordered = [sample for sample in ordered if sample.at <= end]
    return ordered


def valid_values(samples: list[Sample]) -> list[float]:
    """The real temperature readings; offline gaps are skipped, never zeroed."""

    return [sample.value for sample in windowed(samples) if sample.valid]  # type: ignore[misc]


@dataclass(frozen=True)
class CableStats:
    """Every projection of one cable's series under one shared window."""

    cable_id: str
    current: float | None
    status: str
    history: tuple[float, ...]
    average: float
    maximum: float
    minimum: float
    over: int
    samples: int
    gaps: int
    window_start: str | None = None
    window_end: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.cable_id,
            "current": self.current,
            "status": self.status,
            "history": list(self.history),
            "average": self.average,
            "max": self.maximum,
            "min": self.minimum,
            "over": self.over,
            "samples": self.samples,
            "gaps": self.gaps,
            "window_start": self.window_start,
            "window_end": self.window_end,
        }


def cable_stats(
    cable_id: str,
    samples: list[Sample],
    threshold: float,
    start: str | None = None,
    end: str | None = None,
) -> CableStats:
    """Compute every per-cable figure from the same windowed series."""

    window = windowed(samples, start, end)
    values = [sample.value for sample in window if sample.valid]
    gaps = sum(1 for sample in window if sample.quality == QUALITY_GAP)
    current = values[-1] if values else None
    return CableStats(
        cable_id=cable_id,
        current=current,
        status=status_for(current) if current is not None else "unknown",
        history=tuple(float(value) for value in values),
        average=sum(values) / len(values) if values else 0.0,
        maximum=max(values) if values else 0.0,
        minimum=min(values) if values else 0.0,
        over=sum(1 for value in values if value >= threshold),
        samples=len(values),
        gaps=gaps,
        window_start=start,
        window_end=end,
    )


@dataclass(frozen=True)
class StatsRun:
    """One immutable, self describing statistics result for the whole fleet.

    The policy fields record the exact caliber used, so a persisted run and a
    CSV export of that run can always be audited against the same definition.
    """

    id: str
    at: str
    threshold: float
    window_start: str | None
    window_end: str | None
    gap_policy: str
    ordering: str
    cables: tuple[CableStats, ...] = field(default_factory=tuple)

    def for_cable(self, cable_id: str) -> CableStats | None:
        for stats in self.cables:
            if stats.cable_id == cable_id:
                return stats
        return None

    @property
    def total_cables(self) -> int:
        return len(self.cables)

    @property
    def sample_count(self) -> int:
        return sum(stats.samples for stats in self.cables)

    @property
    def gap_count(self) -> int:
        return sum(stats.gaps for stats in self.cables)

    @property
    def danger_count(self) -> int:
        return sum(1 for stats in self.cables if stats.status == "over")

    @property
    def healthy_count(self) -> int:
        return sum(1 for stats in self.cables if stats.status == "normal")

    @property
    def unknown_count(self) -> int:
        return sum(1 for stats in self.cables if stats.status == "unknown")

    @property
    def load_factor(self) -> float:
        known = [stats.current for stats in self.cables if stats.current is not None]
        return sum(known) / len(known) if known else 0.0

    def policy(self) -> dict[str, object]:
        return {
            "threshold": self.threshold,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "gap_policy": self.gap_policy,
            "ordering": self.ordering,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "at": self.at,
            "policy": self.policy(),
            "total": self.total_cables,
            "samples": self.sample_count,
            "gaps": self.gap_count,
            "danger": self.danger_count,
            "healthy": self.healthy_count,
            "unknown": self.unknown_count,
            "load_factor": self.load_factor,
            "cables": [stats.as_dict() for stats in self.cables],
        }


def build_run(
    run_id: str,
    at: str,
    series_by_cable: dict[str, list[Sample]],
    threshold: float,
    start: str | None = None,
    end: str | None = None,
) -> StatsRun:
    """Roll every cable's series into one fleet wide stats run, one caliber."""

    per_cable = tuple(
        cable_stats(cable_id, series_by_cable[cable_id], threshold, start, end)
        for cable_id in sorted(series_by_cable)
    )
    return StatsRun(
        id=run_id,
        at=at,
        threshold=threshold,
        window_start=start,
        window_end=end,
        gap_policy="skip",
        ordering="sample_time",
        cables=per_cable,
    )
