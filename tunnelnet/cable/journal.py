"""Crash safe on disk journal of the cable sampling series.

Each cable owns one JSON document holding its samples in append order.  Every
accept is written through a sibling temporary file and atomically moved into
place (the same guarantee the record store gives), so a crash mid write leaves
the previously committed series intact.  On boot the journal is replayed into
the registry: replay is idempotent (tokens and sample times de-duplicate),
which is also what makes a partial batch failure recoverable - the caller can
redrive the missing samples and the committed ones are ignored.
"""

from __future__ import annotations

import json
import os
import threading

from tunnelnet.store.record import record_ids, record_path

from .series import Sample

KIND = "cable-samples"


class SampleJournal:
    """One atomic JSON file per cable, written before the in-memory commit."""

    def __init__(self, root: str, kind: str = KIND) -> None:
        # The journal lives under the same store root as the other record kinds.
        # The directory is created lazily on the first write so simply opening
        # the service does not surface an empty kind in the store catalogue.
        self._root = os.path.abspath(root)
        self._kind = kind
        self._lock = threading.RLock()

    def cable_ids(self) -> list[str]:
        with self._lock:
            return record_ids(self._root, self._kind)

    def append(self, sample: Sample) -> None:
        """Durably append one sample, preserving the existing series."""

        with self._lock:
            samples = self.load(sample.cable_id)
            samples.append(sample)
            self._write(sample.cable_id, samples)

    def load(self, cable_id: str) -> list[Sample]:
        with self._lock:
            path = record_path(self._root, self._kind, cable_id)
            try:
                with open(path, encoding="utf-8") as handle:
                    payload = json.load(handle)
            except FileNotFoundError:
                return []
            if not isinstance(payload, dict):
                raise ValueError(f"cable journal for {cable_id} is not an object")
            entries = payload.get("samples")
            if not isinstance(entries, list):
                raise ValueError(f"cable journal for {cable_id} has no sample list")
            return [Sample.from_dict(entry) for entry in entries if isinstance(entry, dict)]

    def _write(self, cable_id: str, samples: list[Sample]) -> None:
        path = record_path(self._root, self._kind, cable_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        document = {
            "cable_id": cable_id,
            "ordering": "sample_time",
            "gap_policy": "skip",
            "samples": [sample.as_dict() for sample in samples],
        }
        temporary = f"{path}.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(temporary, path)
