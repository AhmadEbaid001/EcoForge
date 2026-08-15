"""Virtual sensor nodes: one publisher per building, all on one process.

Each reading is published as its own MQTT message in exactly the shape a physical
ESP32 node with a clamp-on CT would send:

    topic:   gemp/reading/<building_id>
    payload: {"building_id": "b001", "ts": "...", "kw": 41.2, "source": "sim"}

Note what is NOT in the payload: the sequence number. Sequence is assigned by the
ingester, which owns the hash chain. A device cannot be trusted to number its own
readings monotonically across a reboot, and letting it try would put chain integrity
at the mercy of the least reliable component in the system.

The data clock runs at GEMP_SIM_SPEED times wall time, starting from wherever the
seed backfill left off, so the live stream is a continuation of the same process
rather than a second one with a discontinuity at the join.

    python -m gemp.sim.node
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import signal
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import paho.mqtt.client as mqtt

from gemp.config import get_settings
from gemp.domain.models import Building
from gemp.domain.portfolio import load_buildings
from gemp.sim.profiles import annual_scale, point_kw

log = logging.getLogger("gemp.sim.node")

STEP_MINUTES = 15
ANCHOR_DIR = Path("anchor")

# Chance per building per emitted reading that a fault begins. Tuned so a fifty
# building portfolio shows something within a couple of minutes of demonstration
# time without the map turning into a wall of red.
ANOMALY_START_P = 0.0006


class LiveAnomaly:
    """A fault currently in progress at one building."""

    __slots__ = ("kind", "multiplier", "until", "held_kw")

    def __init__(self, kind: str, multiplier: float, until: datetime, held_kw: float):
        self.kind = kind
        self.multiplier = multiplier
        self.until = until
        self.held_kw = held_kw

    def apply(self, kw: float) -> float:
        return self.held_kw if self.kind == "flatline" else kw * self.multiplier


class SimulatorNode:
    def __init__(self, buildings: list[Building], settings, seed: int = 20260814):
        self.buildings = buildings
        self.settings = settings
        self.rng = random.Random(seed)
        self.np_rng = np.random.default_rng(seed)
        self.scales = {b.id: annual_scale(b) for b in buildings}
        self.active: dict[str, LiveAnomaly] = {}
        self.published = 0
        self.running = True

        ANCHOR_DIR.mkdir(parents=True, exist_ok=True)
        self.truth_path = ANCHOR_DIR / "live_anomalies.jsonl"

        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id="gemp-sim", clean_session=True
        )
        self.client.username_pw_set(
            settings.mqtt_user, settings.mqtt_password.get_secret_value()
        )
        self.client.on_connect = self._on_connect

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            log.info("connected to broker at %s:%s", self.settings.mqtt_host,
                     self.settings.mqtt_port)
        else:
            log.error("broker refused connection: %s", reason_code)

    def connect(self) -> None:
        self.client.connect(self.settings.mqtt_host, self.settings.mqtt_port, keepalive=60)
        self.client.loop_start()

    def stop(self, *_args) -> None:
        self.running = False

    # -- anomaly injection ---------------------------------------------------

    def _maybe_start_anomaly(self, building: Building, ts: datetime, kw: float) -> None:
        if building.id in self.active or self.rng.random() >= ANOMALY_START_P:
            return

        kind = self.rng.choice(("stuck_on", "spike", "drift", "flatline"))
        hours = {"stuck_on": (4, 12), "spike": (1, 2), "drift": (24, 72), "flatline": (3, 10)}
        low, high = hours[kind]
        multiplier = {
            "stuck_on": self.rng.uniform(1.6, 2.6),
            "spike": self.rng.uniform(2.5, 4.0),
            "drift": self.rng.uniform(1.25, 1.6),
            "flatline": 1.0,
        }[kind]

        anomaly = LiveAnomaly(
            kind=kind,
            multiplier=multiplier,
            until=ts + timedelta(hours=self.rng.uniform(low, high)),
            held_kw=kw,
        )
        self.active[building.id] = anomaly

        # Ground truth, appended as it happens so precision and recall stay reportable
        # for the live stream and not only for the seeded history.
        with self.truth_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "building_id": building.id,
                "kind": kind,
                "start": ts.isoformat(),
                "end": anomaly.until.isoformat(),
                "magnitude": multiplier,
            }) + "\n")
        log.info("injected %s at %s until %s", kind, building.code, anomaly.until)

    def _apply_anomaly(self, building: Building, ts: datetime, kw: float) -> float:
        anomaly = self.active.get(building.id)
        if anomaly is None:
            self._maybe_start_anomaly(building, ts, kw)
            return kw
        if ts >= anomaly.until:
            del self.active[building.id]
            return kw
        return anomaly.apply(kw)

    # -- main loop -----------------------------------------------------------

    def run(self, data_start: datetime, max_readings: int | None = None) -> int:
        settings = self.settings
        step = timedelta(minutes=STEP_MINUTES)
        data_now = data_start
        wall_start = time.monotonic()

        log.info(
            "simulating %d buildings from %s at %dx (1 wall-second = %d data-minutes)",
            len(self.buildings), data_start.isoformat(), settings.sim_speed,
            settings.sim_speed // 60,
        )

        while self.running:
            elapsed = time.monotonic() - wall_start
            data_target = data_start + timedelta(seconds=elapsed * settings.sim_speed)

            while data_now <= data_target and self.running:
                for building in self.buildings:
                    kw = point_kw(building, data_now, scale=self.scales[building.id],
                                  rng=self.np_rng)
                    kw = self._apply_anomaly(building, data_now, kw)
                    self._publish(building, data_now, kw)

                data_now += step
                if max_readings is not None and self.published >= max_readings:
                    self.running = False

            time.sleep(min(settings.sim_interval_s, 1.0))

        self.client.loop_stop()
        self.client.disconnect()
        return self.published

    def _publish(self, building: Building, ts: datetime, kw: float) -> None:
        payload = json.dumps({
            "building_id": building.id,
            "ts": ts.isoformat(timespec="seconds"),
            "kw": round(float(kw), 4),
            "source": "sim",
        })
        self.client.publish(
            f"{self.settings.mqtt_topic_prefix}/{building.id}", payload, qos=1
        )
        self.published += 1


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s  %(message)s"
    )
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="data_start", default=None,
                        help="ISO timestamp to start the data clock (default: now)")
    parser.add_argument("--max-readings", type=int, default=None,
                        help="stop after publishing this many (for smoke tests)")
    args = parser.parse_args(argv)

    settings = get_settings()
    buildings = load_buildings()
    start = (
        datetime.fromisoformat(args.data_start)
        if args.data_start
        else datetime.now(UTC).replace(second=0, microsecond=0)
    )

    node = SimulatorNode(buildings, settings)
    signal.signal(signal.SIGINT, node.stop)
    signal.signal(signal.SIGTERM, node.stop)

    try:
        node.connect()
    except OSError as exc:
        log.error("cannot reach broker at %s:%s - %s",
                  settings.mqtt_host, settings.mqtt_port, exc)
        return 1

    published = node.run(start, args.max_readings)
    log.info("published %d readings", published)
    return 0


if __name__ == "__main__":
    sys.exit(main())
