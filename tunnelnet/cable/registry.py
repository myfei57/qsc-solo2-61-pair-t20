"""Thread safe registry of monitored cable sections.

One sample series per cable is the single source of truth: the history
window, the current temperature and the over-temperature counts all derive
from it, so the report and the list views cannot disagree.

A sample is persisted before it becomes visible: the series document is
written to the store first and only then applied in memory, so statistics
can never observe a sample that was not durably stored.  A failed write
rejects the sample and leaves memory untouched; a restart replays exactly
the persisted series and recomputes the derived statistics.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from tunnelnet.store import Store

from .model import OVER_TEMPERATURE, Cable, status_for
from .report import CableState, CableSummary, cable_state
from .series import SampleSeries, utc_now
from .statistics import cable_statistics

SAMPLE_KIND = "cable-sample"
STATS_KIND = "cable-stats"


class CableRegistry:
    """Holds cables and the persisted sample series their stats derive from."""

    def __init__(self, store: Store | None = None, clock: Callable[[], str] = utc_now) -> None:
        self._items: dict[str, Cable] = {}
        self._series: dict[str, SampleSeries] = {}
        self._store = store
        self._clock = clock
        self._lock = threading.RLock()
        self.restore()

    def restore(self) -> None:
        """Replay every persisted series, losing nothing that was stored."""

        if self._store is None:
            return
        with self._lock:
            for cable_id in self._store.list(SAMPLE_KIND):
                series = SampleSeries.from_dict(self._store.load(SAMPLE_KIND, cable_id))
                if series is not None and series.count():
                    self._series[cable_id] = series

    def add(self, cable: Cable) -> Cable:
        with self._lock:
            series = self._series.get(cable.id)
            if series is not None and series.count():
                current = series.current()
                cable.temp = current.value
            cable.status = status_for(cable.temp)
            self._items[cable.id] = cable
            self._series.setdefault(cable.id, SampleSeries())
            return cable

    def get(self, cable_id: str) -> CableState | None:
        with self._lock:
            cable = self._items.get(cable_id)
            if cable is None:
                return None
            return cable_state(cable)

    def set_temp(self, cable_id: str, temp: float) -> bool:
        """Record a temperature; goes through the same series as any sample."""

        return self.sample(cable_id, temp)

    def list(self) -> list[CableState]:
        with self._lock:
            return [cable_state(self._items[key]) for key in sorted(self._items)]

    def count(self) -> int:
        with self._lock:
            return len(self._items)

    def sample(self, cable_id: str, value: float, at: str = "") -> bool:
        """Store one reading, then make it visible to every statistic."""

        with self._lock:
            cable = self._items.get(cable_id)
            if cable is None:
                return False
            series = self._series.setdefault(cable_id, SampleSeries())
            candidate = series.copy()
            candidate.record(value, at or self._clock())
            self._save_series(cable_id, candidate)
            self._series[cable_id] = candidate
            current = candidate.current()
            cable.temp = current.value
            cable.status = status_for(current.value)
            self._save_stats(cable_id)
            return True

    def _save_series(self, cable_id: str, series: SampleSeries) -> None:
        if self._store is None:
            return
        payload = series.as_dict()
        payload["cable_id"] = cable_id
        self._store.save(SAMPLE_KIND, cable_id, payload)

    def _save_stats(self, cable_id: str) -> None:
        if self._store is None:
            return
        stats = cable_statistics(self, cable_id, OVER_TEMPERATURE, self._clock())
        self._store.save(STATS_KIND, cable_id, stats.as_dict())

    def history(self, cable_id: str) -> list[float]:
        with self._lock:
            series = self._series.get(cable_id)
            return series.values() if series is not None else []

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

    def sample_count(self) -> int:
        with self._lock:
            return sum(series.count() for series in self._series.values())

    def summary(self) -> CableSummary:
        return CableSummary(total=self.count(), samples=self.sample_count())

    def temperatures(self) -> list[float]:
        """Current temperature per cable, each derived from its series."""

        with self._lock:
            return [self._items[key].temp for key in sorted(self._items)]

    def describe(self) -> str:
        return f"cables total={self.count()} samples={self.sample_count()}"
