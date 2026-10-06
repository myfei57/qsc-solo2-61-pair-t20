"""Thread safe registry of cables and their sampling series.

The registry holds the cable catalogue and, per cable, exactly one sampling
series.  There is deliberately no separate "current temperature" slot and no
separate history buffer: the current state and the history window are both
projections of the series (see :mod:`tunnelnet.cable.stats`).

Samples are accepted in any arrival order and inserted by sample time, so late
arrivals cannot re-order the series.  A repeated delivery (same token, or same
cable/sample time) is de-duplicated, which makes retry and replay idempotent.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from .model import Cable, status_for
from .report import CableState, CableSummary
from .series import QUALITY_GAP, Sample, utc_timestamp
from .stats import CableStats, cable_stats


class CableRegistry:
    """Holds cables and the single timestamped series belonging to each one."""

    def __init__(self, clock: Callable[[], str] = utc_timestamp) -> None:
        self._items: dict[str, Cable] = {}
        self._series: dict[str, list[Sample]] = {}
        self._tokens: dict[str, set[str]] = {}
        self._seen_at: dict[str, set[str]] = {}
        self._next_seq = 0
        self._clock = clock
        self._lock = threading.RLock()

    # -- catalogue ---------------------------------------------------------

    def add(self, cable: Cable) -> Cable:
        with self._lock:
            cable.status = status_for(cable.temp)
            self._items[cable.id] = cable
            self._series.setdefault(cable.id, [])
            self._tokens.setdefault(cable.id, set())
            self._seen_at.setdefault(cable.id, set())
            return cable

    def get(self, cable_id: str) -> CableState | None:
        with self._lock:
            cable = self._items.get(cable_id)
            if cable is None:
                return None
            current = self._current(cable_id)
            temp = cable.temp if current is None else current
            return CableState(
                id=cable.id,
                cabin=cable.cabin,
                temp=temp,
                status=status_for(temp) if current is not None else cable.status,
            )

    def list(self) -> list[CableState]:
        with self._lock:
            return [state for key in sorted(self._items) if (state := self.get(key)) is not None]

    def count(self) -> int:
        with self._lock:
            return len(self._items)

    def cable_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._items)

    # -- sampling series (the single source of truth) ----------------------

    def record(
        self,
        cable_id: str,
        value: float,
        at: str | None = None,
        quality: str = "ok",
        token: str = "",
    ) -> Sample | None:
        """Add one ordered, de-duplicated sample; ``None`` when it is a repeat.

        ``value`` is ignored for gap samples, which mark an offline period that
        every statistic skips.  A missing ``at`` is stamped by the registry
        clock, so locally injected samples still order by sample time.
        """

        with self._lock:
            if cable_id not in self._items:
                return None
            stamp = self._clock() if not at else at
            if token and token in self._tokens[cable_id]:
                return None
            if stamp in self._seen_at[cable_id]:
                # Same cable and sample time is a redelivery of one observation.
                return None
            self._next_seq += 1
            sample = Sample(
                cable_id=cable_id,
                value=None if quality == QUALITY_GAP else float(value),
                at=stamp,
                seq=self._next_seq,
                quality=quality,
                token=token,
            )
            if token:
                self._tokens[cable_id].add(token)
            self._seen_at[cable_id].add(stamp)
            self._series[cable_id].append(sample)
            self._series[cable_id].sort(key=Sample.order_key)
            return sample

    def ingest(self, sample: Sample) -> Sample | None:
        """Replay an already persisted sample, preserving its idempotency."""

        with self._lock:
            if sample.cable_id not in self._items:
                return None
            if sample.token and sample.token in self._tokens[sample.cable_id]:
                return None
            if sample.at in self._seen_at[sample.cable_id]:
                return None
            if sample.token:
                self._tokens[sample.cable_id].add(sample.token)
            self._seen_at[sample.cable_id].add(sample.at)
            self._next_seq = max(self._next_seq, sample.seq)
            self._series[sample.cable_id].append(sample)
            self._series[sample.cable_id].sort(key=Sample.order_key)
            return sample

    def record_gap(self, cable_id: str, at: str | None = None, token: str = "") -> Sample | None:
        """Mark an offline period; statistics skip it rather than zero filling."""

        return self.record(cable_id, 0.0, at=at, quality=QUALITY_GAP, token=token)

    def set_temp(self, cable_id: str, temp: float) -> bool:
        """Backwards compatible setter, now an observation on the series."""

        return self.record(cable_id, temp) is not None

    def sample(self, cable_id: str, value: float) -> bool:
        """Backwards compatible un-timestamped sampling hook."""

        return self.record(cable_id, value) is not None

    # -- projections (all read the one series) -----------------------------

    def series(self, cable_id: str) -> list[Sample]:
        with self._lock:
            return list(self._series.get(cable_id, []))

    def replace_series(self, cable_id: str, samples: list[Sample]) -> None:
        """Reset one cable to an authoritative durable series (crash rollback)."""

        with self._lock:
            if cable_id not in self._items:
                return
            ordered = sorted(samples, key=Sample.order_key)
            self._series[cable_id] = ordered
            self._tokens[cable_id] = {sample.token for sample in ordered if sample.token}
            self._seen_at[cable_id] = {sample.at for sample in ordered}
            self._next_seq = max([self._next_seq, *(sample.seq for sample in ordered)])

    def all_series(self) -> dict[str, list[Sample]]:
        with self._lock:
            return {cable_id: list(samples) for cable_id, samples in self._series.items()}

    def history(self, cable_id: str) -> list[float]:
        """Valid readings in sample time order; gaps skipped."""

        return [sample.value for sample in self.series(cable_id) if sample.valid]  # type: ignore[misc]

    def _current(self, cable_id: str) -> float | None:
        values = self.history(cable_id)
        return values[-1] if values else None

    def current(self, cable_id: str) -> float | None:
        return self._current(cable_id)

    def average(self, cable_id: str) -> float:
        values = self.history(cable_id)
        return sum(values) / len(values) if values else 0.0

    def maximum(self, cable_id: str) -> float:
        values = self.history(cable_id)
        return max(values) if values else 0.0

    def minimum(self, cable_id: str) -> float:
        values = self.history(cable_id)
        return min(values) if values else 0.0

    def over(self, cable_id: str, threshold: float) -> int:
        return sum(1 for value in self.history(cable_id) if value >= threshold)

    def stats(
        self,
        cable_id: str,
        threshold: float,
        start: str | None = None,
        end: str | None = None,
    ) -> CableStats:
        """Every per-cable figure from the same windowed series."""

        return cable_stats(cable_id, self.series(cable_id), threshold, start, end)

    def sample_count(self) -> int:
        with self._lock:
            return sum(
                1 for samples in self._series.values() for sample in samples if sample.valid
            )

    def gap_count(self) -> int:
        with self._lock:
            return sum(
                1 for samples in self._series.values() for sample in samples if not sample.valid
            )

    def summary(self) -> CableSummary:
        return CableSummary(total=self.count(), samples=self.sample_count())

    def temperatures(self) -> list[float]:
        with self._lock:
            result = []
            for key in sorted(self._items):
                current = self._current(key)
                if current is not None:
                    result.append(current)
            return result

    def describe(self) -> str:
        return (
            f"cables total={self.count()} samples={self.sample_count()} gaps={self.gap_count()}"
        )
