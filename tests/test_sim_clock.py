"""The data clock has a ceiling.

`sim_speed` on its own is a one-way ratchet. The simulator resumes from the newest
stored reading, so restarting continues the climb rather than resetting it, and at
720x one wall day is two data years. A deployment left running for ten days had
readings dated 2047 against a wall clock reading 2026 - and a database that had
grown to 35.7 million rows getting there.

These tests pin the ceiling rather than the speed. The acceleration is still
wanted: it is what carries the clock through backfill quickly. What is not wanted
is acceleration continuing once it has caught up.

The offsets here are seconds, not days, and that is deliberate. The loop advances
by wall time times sim_speed no matter how short its sleep is, so a fortnight of
backfill at 720x takes 28 real minutes to cross - a test that starts a fortnight
back does not exercise the clamp, it just runs for half an hour.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

from tests.conftest import make_building

from gemp.config import Settings
from gemp.sim.node import SimulatorNode


class _SilentClient:
    """Stands in for the MQTT client. The node only ever calls these three."""

    def publish(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        pass

    def loop_stop(self) -> None:
        pass

    def disconnect(self) -> None:
        pass


def _node(clamp: bool) -> tuple[SimulatorNode, list[datetime]]:
    settings = Settings(
        sim_speed=720,
        sim_interval_s=0.01,
        sim_clamp_to_wall_clock=clamp,
    )
    node = SimulatorNode([make_building()], settings)
    node.client = _SilentClient()

    stamps: list[datetime] = []
    node._publish = lambda building, ts, kw: stamps.append(ts)  # noqa: SLF001
    return node, stamps


def _run_briefly(node: SimulatorNode, start: datetime, seconds: float = 1.5) -> None:
    """Run the loop for a moment and then ask it to stop.

    `max_readings` cannot end a run that publishes nothing, which is exactly the
    case one of these tests is about, so the bound has to be wall time.
    """
    thread = threading.Thread(target=node.run, args=(start,), daemon=True)
    thread.start()
    thread.join(timeout=seconds)
    node.stop()
    thread.join(timeout=5)


def test_the_clock_stops_at_the_wall_clock() -> None:
    """Starting behind, it catches up and then does not overshoot.

    Two minutes of backfill is 0.17 wall-seconds at 720x, so the interesting part
    of the run - the part after it has caught up - is nearly all of it.
    """
    node, stamps = _node(clamp=True)
    start = datetime.now(UTC) - timedelta(minutes=120)

    _run_briefly(node, start)

    assert stamps, "the backfill should publish"
    newest = max(stamps)
    assert newest <= datetime.now(UTC), (
        f"published a reading dated {newest.isoformat()}, which is in the future"
    )


def test_a_clock_already_in_the_future_publishes_nothing() -> None:
    """The state the staging deployment was actually in.

    The clamp cannot undo history that is already ahead of the wall clock - only a
    re-seed can - so the correct behaviour is to add nothing to it. The node logs
    the remedy at startup; what matters here is that it does not quietly extend
    the drift.
    """
    node, stamps = _node(clamp=True)
    start = datetime.now(UTC) + timedelta(days=365)

    _run_briefly(node, start)

    assert stamps == [], (
        "a simulator whose stored history is in the future must not add to it"
    )


def test_the_ceiling_can_be_lifted_deliberately() -> None:
    """Long-horizon replay is still possible, but it has to be asked for.

    The default is the safe one; this is the escape hatch, and the test exists so
    that turning the clamp off keeps meaning what it says.
    """
    node, stamps = _node(clamp=False)
    start = datetime.now(UTC) - timedelta(minutes=1)

    _run_briefly(node, start)

    assert stamps, "unclamped, it should publish"
    assert max(stamps) > datetime.now(UTC), (
        "with the ceiling lifted the clock is expected to run past the present"
    )
