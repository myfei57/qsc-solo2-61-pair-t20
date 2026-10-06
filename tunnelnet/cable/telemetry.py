"""Cable telemetry service: one sampling series, one statistics caliber.

The service enforces the timing dependency between sampling and statistics:

1. Every observation is journaled first and then committed to the in-memory
   series, so the series on disk and the series in memory cannot disagree and a
   crash is recovered by replaying the journal on boot.
2. A statistics run is computed from the committed series and returned as one
   immutable snapshot.  A durable run is persisted itself and an audit record is
   appended in the same operation; the JSON views, the health check and the CSV
   export all read that same run, so the over temperature count can never be
   computed twice from two different inputs.

Out of order samples are ordered by sample time inside the registry; partial
batch failures leave the accepted durable prefix behind and report the rejected
samples for redrive.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable

from tunnelnet.audit import Record
from tunnelnet.store import Store

from .journal import SampleJournal
from .model import OVER_TEMPERATURE
from .registry import CableRegistry
from .series import QUALITY_GAP, Sample, utc_timestamp
from .stats import StatsRun, build_run

STATS_KIND = "cable-stats"


class AuditSink:
    """Protocol satisfied by :class:`~tunnelnet.audit.AuditRegistry`."""

    def append(self, record: Record) -> Record:  # pragma: no cover - structural
        ...


class IngestResult:
    """Outcome of one ingest call: what was accepted and what must redrive."""

    def __init__(self, accepted: list[Sample], rejected: list[dict[str, object]]) -> None:
        self.accepted = accepted
        self.rejected = rejected

    @property
    def ok(self) -> bool:
        return not self.rejected

    def as_dict(self) -> dict[str, object]:
        return {
            "accepted": [sample.as_dict() for sample in self.accepted],
            "rejected": self.rejected,
            "count": len(self.accepted),
        }


class CableTelemetry:
    """Owns the cable registry, its journal and the latest statistics run."""

    def __init__(
        self,
        store: Store,
        audit: AuditSink,
        threshold: float = OVER_TEMPERATURE,
        registry: CableRegistry | None = None,
        journal: SampleJournal | None = None,
        clock: Callable[[], str] = utc_timestamp,
    ) -> None:
        self._store = store
        self._audit = audit
        self._threshold = threshold
        self._clock = clock
        self.registry = registry or CableRegistry(clock=clock)
        self._journal = journal or SampleJournal(store.root)
        self._latest: StatsRun | None = None
        self._lock = threading.RLock()

    @property
    def threshold(self) -> float:
        return self._threshold

    def set_threshold(self, value: float) -> float:
        self._threshold = value
        return self._threshold

    # -- recovery ----------------------------------------------------------

    def recover(self) -> int:
        """Replay every durable journal entry into the registry.

        Idempotent: entries already in the registry (replayed or redelivered)
        are skipped.  Returns the number of samples restored.
        """

        restored = 0
        with self._lock:
            for cable_id in self._journal.cable_ids():
                for sample in self._journal.load(cable_id):
                    if self.registry.ingest(sample) is not None:
                        restored += 1
        return restored

    # -- sampling ----------------------------------------------------------

    def record_seed(
        self,
        cable_id: str,
        value: float,
        at: str | None = None,
        quality: str = "ok",
        token: str = "",
    ) -> Sample | None:
        """Add a boot time observation in memory without journaling it.

        Demo/seed values are regenerated on every boot, so they must not create
        durable records.  Real deliveries go through :meth:`ingest_sample`.
        """

        with self._lock:
            stamp = self._clock() if not at else at
            sample = self.registry.record(
                cable_id, value, at=stamp, quality=quality, token=token
            )
            if sample is not None:
                self._latest = self._build_current()
            return sample

    def ingest_sample(
        self,
        cable_id: str,
        value: float,
        at: str | None = None,
        quality: str = "ok",
        token: str = "",
    ) -> Sample | None:
        """Durably accept one sample, or return ``None`` when it is a repeat."""

        with self._lock:
            return self._accept(cable_id, value, at, quality, token)

    def ingest_gap(self, cable_id: str, at: str | None = None, token: str = "") -> Sample | None:
        """Durably record an offline gap which statistics skip."""

        with self._lock:
            return self._accept(cable_id, 0.0, at, QUALITY_GAP, token)

    def _accept(
        self,
        cable_id: str,
        value: float,
        at: str | None,
        quality: str,
        token: str,
    ) -> Sample | None:
        stamp = self._clock() if not at else at
        sample = self.registry.record(
            cable_id, value, at=stamp, quality=quality, token=token
        )
        if sample is None:
            return None
        try:
            self._journal.append(sample)
        except Exception:
            # The in-memory copy must not advertise a sample that never reached
            # disk; surface the failure so the caller can redrive it.
            self._rollback(sample)
            raise
        # Keep the shared run current so the report, list and health check
        # already reflect this commit; publish() is what durably audits it.
        self._latest = self._build_current()
        return sample

    def _rollback(self, sample: Sample) -> None:
        # Rebuild the affected cable series from the durable journal, which is
        # the authority after a failed journal write.
        durable = self._journal.load(sample.cable_id)
        self.registry.replace_series(sample.cable_id, durable)

    def ingest_batch(self, requests: list[dict[str, object]]) -> IngestResult:
        """Accept many samples; failures are isolated and reported for redrive."""

        accepted: list[Sample] = []
        rejected: list[dict[str, object]] = []
        with self._lock:
            for request in requests:
                cable_id = str(request.get("cable_id", ""))
                try:
                    raw_value = request.get("value")
                    quality = str(request.get("quality", "ok"))
                    value = 0.0 if raw_value is None else float(raw_value)
                    sample = self._accept(
                        cable_id,
                        value,
                        _optional_str(request.get("at")),
                        quality,
                        str(request.get("token", "")),
                    )
                except (TypeError, ValueError) as exc:
                    rejected.append({"cable_id": cable_id, "reason": str(exc), "request": request})
                    continue
                if sample is None:
                    rejected.append(
                        {"cable_id": cable_id, "reason": "unknown cable or duplicate sample", "request": request}
                    )
                else:
                    accepted.append(sample)
        return IngestResult(accepted, rejected)

    # -- statistics --------------------------------------------------------

    def _build_current(
        self, start: str | None = None, end: str | None = None
    ) -> StatsRun:
        return build_run(
            run_id=self._new_run_id(),
            at=self._clock(),
            series_by_cable=self.registry.all_series(),
            threshold=self._threshold,
            start=start,
            end=end,
        )

    def compute(
        self,
        start: str | None = None,
        end: str | None = None,
        *,
        remember: bool = True,
    ) -> StatsRun:
        """Build one immutable run from the current committed series."""

        with self._lock:
            run = self._build_current(start, end)
            if remember and start is None and end is None:
                self._latest = run
            return run

    def publish(self, start: str | None = None, end: str | None = None) -> StatsRun:
        """Compute a run, persist it and append its audit record together.

        Each document is written atomically; the audit entry carries the run
        id so the event and the result document can be cross checked.  Every
        console view and export references this run, giving the report, the
        list and the over temperature count one shared, audited caliber.
        """

        with self._lock:
            run = self.compute(start, end, remember=start is None and end is None)
            self._store.save(STATS_KIND, "latest", run.as_dict())
            self._store.save(STATS_KIND, run.id, run.as_dict())
            # The audit entry carries the run id so a result can be traced back
            # to the exact series snapshot and caliber it was computed from.
            self._audit.append(
                Record(id=uuid.uuid4().hex, event=f"cable_stats:{run.id}", at=run.at)
            )
            return run

    def latest(self, *, publish_if_none: bool = False) -> StatsRun:
        """The current shared run, optionally materializing the first one."""

        with self._lock:
            if self._latest is not None:
                return self._latest
            if publish_if_none:
                return self.publish()
            self._latest = self._build_current()
            return self._latest

    def cable_stats(self, cable_id: str, start: str | None = None, end: str | None = None):
        if start is None and end is None:
            stats = self.latest().for_cable(cable_id)
            if stats is not None:
                return stats
        return self.registry.stats(cable_id, self._threshold, start, end)

    def _new_run_id(self) -> str:
        return f"{self._clock()}-{uuid.uuid4().hex[:8]}"


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None
