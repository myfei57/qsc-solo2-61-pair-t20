"""Default values written on first boot."""

from __future__ import annotations

from tunnelnet.cabin import Cabin
from tunnelnet.cable import Cable
from tunnelnet.drain import Pit
from tunnelnet.fire import Detector

from .runtime import Runtime

SEED_AT = "2026-01-01T00:00:00Z"
#: Deterministic seed sample times, ordered, so the shared series is reproducible.
SEED_SAMPLE_AT = (
    "2026-01-01T00:00:00.000001Z",
    "2026-01-01T00:00:00.000002Z",
)


def seed_topology(rt: Runtime) -> None:
    """Register the catalogue every component needs.

    Runs before journal recovery so replayed samples can always find their
    cable.  Idempotent: registering the same catalogue twice changes nothing.
    """

    rt.namespaces.add("segment-a", "segment A")
    rt.namespaces.add("segment-b", "segment B")

    for index, (cabin_id, zone, partition) in enumerate(
        (("cabin-1", "zone-1", "part-1"), ("cabin-2", "zone-2", "part-2"), ("cabin-3", "zone-3", "part-3")),
        start=1,
    ):
        rt.cabins.upsert(
            Cabin(id=cabin_id, name=f"cabin {index}", zone=zone, partition=partition, revision=index)
        )
        rt.partitions.repartition(partition, cabin_id)
        rt.env.bind_sensor_at(f"sensor-{index}", partition)

    rt.drain.add_pit(Pit(id="pit-1", cabin="cabin-1", water_level=0.2))
    rt.drain.add_pit(Pit(id="pit-2", cabin="cabin-2", water_level=0.1))

    rt.fire.add_detector(Detector(id="detector-1", zone="zone-1"))
    rt.fire.add_detector(Detector(id="detector-2", zone="zone-2"))

    rt.power_book.set_baseline("cabin-1", 4200.0)
    rt.power_book.set_baseline("cabin-2", 3800.0)
    rt.power_book.set_baseline("cabin-3", 4600.0)

    rt.cables.add(Cable(id="cable-1", cabin="cabin-1", temp=42.0))
    rt.cables.add(Cable(id="cable-2", cabin="cabin-2", temp=46.0))

    rt.patrol.rebuild_route()


def seed_observations(rt: Runtime) -> None:
    """Record the demo observations on the shared sampling series."""

    rt.env.sample("sensor-1", 48.0, SEED_AT)
    rt.env.sample("sensor-1", 51.0, SEED_AT)
    rt.env.sample("sensor-2", 55.0, SEED_AT)
    rt.patrol.check_in("cabin-1", SEED_AT)
    rt.patrol.set_due("cabin-2", "2025-12-31T00:00:00Z")
    rt.quota.add_usage("cabin-1", 1200.0)
    rt.quota.add_usage("cabin-2", 900.0)
    rt.drain.record_cycle("cabin-1", SEED_AT)
    rt.drain.record_cycle("cabin-1", SEED_AT)
    rt.cable_telemetry.record_seed("cable-1", 42.0, at=SEED_SAMPLE_AT[0])
    rt.cable_telemetry.record_seed("cable-1", 47.0, at=SEED_SAMPLE_AT[1])
    rt.cable_telemetry.record_seed("cable-2", 46.0, at=SEED_SAMPLE_AT[1])


def seed_defaults(rt: Runtime) -> None:
    """Boot sequence: catalogue, durable recovery, then fresh observations."""

    seed_topology(rt)
    rt.cable_telemetry.recover()
    seed_observations(rt)
