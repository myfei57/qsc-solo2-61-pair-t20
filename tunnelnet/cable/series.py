"""Timestamped cable temperature samples: the single sampling series.

Every cable temperature view (the history window, the current state and the
over temperature counters) is derived from one series of ``Sample`` values.
The series is the only source of truth, so the different projections can never
drift apart.

Two policy choices are fixed here and reused by every statistic:

* Ordering is by *sample time* (``at``), never by arrival time.  A sample that
  arrives late is inserted at its sampling position.  ``seq`` only breaks ties
  for observations stamped in the same clock tick.
* An offline gap is an explicit ``quality == "gap"`` sample and is *skipped*,
  never filled with zero.  A zero would pretend the dead link was a cold
  reading and bias both the average and the over temperature count.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

QUALITY_OK = "ok"
QUALITY_GAP = "gap"

#: Offline gaps are skipped by every statistic (documented on every stats run).
GAP_POLICY = "skip"
#: Samples are ordered by their sampling timestamp (documented on every run).
ORDERING = "sample_time"


def utc_timestamp() -> str:
    """UTC timestamp with microsecond resolution, lexicographically sortable."""

    return (
        time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        + f".{time.time_ns() // 1000 % 1_000_000:06d}Z"
    )


@dataclass(frozen=True)
class Sample:
    """One observation of one cable, or one explicit offline gap."""

    cable_id: str
    value: float | None
    at: str
    seq: int = 0
    quality: str = QUALITY_OK
    token: str = ""

    @property
    def valid(self) -> bool:
        """Whether the sample carries a real temperature reading."""

        return self.quality == QUALITY_OK and self.value is not None

    def order_key(self) -> tuple[str, int]:
        return (self.at, self.seq)

    def as_dict(self) -> dict[str, object]:
        return {
            "cable_id": self.cable_id,
            "value": self.value,
            "at": self.at,
            "seq": self.seq,
            "quality": self.quality,
            "token": self.token,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "Sample":
        value = payload.get("value")
        return cls(
            cable_id=str(payload["cable_id"]),
            value=None if value is None else float(value),
            at=str(payload.get("at", "")),
            seq=int(payload.get("seq", 0)),
            quality=str(payload.get("quality", QUALITY_OK)),
            token=str(payload.get("token", "")),
        )


def order_samples(samples: list[Sample]) -> list[Sample]:
    """Return the samples ordered by sample time, tie broken by insertion seq."""

    return sorted(samples, key=Sample.order_key)
