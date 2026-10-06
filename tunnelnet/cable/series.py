"""The persisted sample sequence every cable statistic derives from.

Two calibre decisions are fixed here so every consumer computes the same
numbers:

* Gaps are *skipped* (``GAP_POLICY``): while a cable is offline no samples
  exist and none are synthesised.  Averages and over-temperature counts
  cover only readings that actually arrived.  Zero filling was rejected
  because it pulls the average towards zero and hides real overheating.
* Samples are ordered by *sample time* (``ORDER_POLICY``): the sequence is
  sorted by ``at`` with the arrival sequence ``seq`` breaking ties, so
  statistics are identical no matter in which order samples arrive.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

GAP_POLICY = "skip"
ORDER_POLICY = "sample_time"


def utc_now() -> str:
    """Current UTC time, same compact form as the drain execution marks."""

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass(frozen=True)
class Sample:
    """One temperature reading with its sample time and arrival sequence."""

    at: str
    value: float
    seq: int

    def as_dict(self) -> dict[str, object]:
        return {"at": self.at, "value": self.value, "seq": self.seq}


class SampleSeries:
    """Samples of one cable, always sorted by ``(at, seq)``."""

    def __init__(self, samples: tuple[Sample, ...] = ()) -> None:
        self._samples = sorted(samples, key=lambda sample: (sample.at, sample.seq))
        self._next_seq = max((sample.seq for sample in self._samples), default=-1) + 1

    def record(self, value: float, at: str) -> Sample:
        """Append one reading, keeping the series sorted by sample time."""

        sample = Sample(at=at, value=float(value), seq=self._next_seq)
        self._next_seq += 1
        self._samples.append(sample)
        self._samples.sort(key=lambda item: (item.at, item.seq))
        return sample

    def copy(self) -> "SampleSeries":
        return SampleSeries(tuple(self._samples))

    def count(self) -> int:
        return len(self._samples)

    def current(self) -> Sample | None:
        """The reading with the latest sample time, whatever arrived last."""

        return self._samples[-1] if self._samples else None

    def samples(self) -> list[Sample]:
        return list(self._samples)

    def values(self) -> list[float]:
        return [sample.value for sample in self._samples]

    def average(self) -> float:
        values = self.values()
        return sum(values) / len(values) if values else 0.0

    def maximum(self) -> float:
        values = self.values()
        return max(values) if values else 0.0

    def minimum(self) -> float:
        values = self.values()
        return min(values) if values else 0.0

    def over(self, threshold: float) -> int:
        return sum(1 for sample in self._samples if sample.value >= threshold)

    def as_dict(self) -> dict[str, object]:
        return {"samples": [sample.as_dict() for sample in self._samples]}

    @classmethod
    def from_dict(cls, payload: dict[str, object] | None) -> "SampleSeries | None":
        """Rebuild a series from its stored document, ``None`` when corrupt."""

        if not isinstance(payload, dict):
            return None
        raw = payload.get("samples", [])
        if not isinstance(raw, list):
            return None
        samples = []
        for item in raw:
            if not isinstance(item, dict):
                return None
            try:
                samples.append(
                    Sample(at=str(item["at"]), value=float(item["value"]), seq=int(item["seq"]))
                )
            except (KeyError, TypeError, ValueError):
                return None
        return cls(tuple(samples))
