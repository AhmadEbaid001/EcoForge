"""F11 - virtual sensor nodes: one publisher per building, all on one process.

F11 cut the WireGuard VPN because the physical sensors it was reserved for do not
exist, and said the paper should describe it as designed-for with the MQTT topic
contract as the evidence. This docstring is that evidence: the contract below is
what a real node would publish, so admitting hardware is a matter of pointing it at
the broker over whatever transport the site provides. Nothing downstream - the
ingester, the hash chain, the forecaster - can tell the difference, because none of
them knows where a message came from.

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
import os
import random
import signal
import sys
import time
import urllib.parse
import urllib.request
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
        # Reproducibility is the requirement here, not unpredictability.
        self.rng = random.Random(seed)  # nosec B311
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


class ResumePointUnavailable(RuntimeError):
    """The API could not be reached, so where to resume the data clock is unknown."""


def resume_point(attempts: int = 30, delay_s: float = 5.0) -> datetime:
    """Where the data clock should start: just after the newest stored reading.

    Starting at wall-clock `now` instead leaves a hole between the seeded history and
    the live stream, because the seed ends at the moment seeding finished while the
    simulator would begin whenever its container happened to start - and under 720x
    replay those drift apart fast. A gap looks like a fleet-wide outage on the
    dashboard and poisons the forecaster's lag features.

    Asks the API rather than the database so the simulator keeps no database
    credentials and stays a pure publisher, exactly as a physical node would be.

    **It retries rather than guessing, and raises rather than falling back to `now`.**
    That fallback cost the project its live evaluation data and was invisible while it
    did so. `depends_on: service_healthy` only holds for `docker compose up`; when the
    API restarts later, `restart: always` brings this container back on its own and
    the health endpoint is refused for a few seconds. The old code answered that with
    `now`, which under 720x replay is over a YEAR BEHIND the stored data - so every
    reading it published was a duplicate that `insert_ignore` dropped, while
    `_maybe_start_anomaly` went on appending faults to the ground-truth file for
    readings that were never stored.

    The damage was entirely silent: the stream looked alive, the container was up, and
    the only symptom was anomaly recall collapsing from 0.81 to 0.26 as the truth file
    filled with events that no data supports. Failing here instead lets Docker restart
    the container until the API is actually back, which is the loop that was wanted.
    """
    base = os.environ.get("GEMP_API_URL", "http://core:8000")
    # urlopen honours file:// and reads the file. GEMP_API_URL is deployment
    # configuration rather than user input, so this is not a forgery hole - but a
    # typo that turns the resume probe into a local file read should say so here,
    # not surface as an inexplicable "no readings" three hundred lines later.
    if urllib.parse.urlparse(base).scheme.lower() not in ("http", "https"):
        raise SystemExit(
            f"GEMP_API_URL is {base!r}, which is not an http or https URL.")
    url = base + "/health"
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            with urllib.request.urlopen(url, timeout=10) as response:  # nosec B310
                latest = json.load(response).get("readings")
        except (OSError, ValueError) as exc:
            last_error = exc
            log.warning("attempt %d/%d: could not read %s (%s)", attempt, attempts, url, exc)
        else:
            if latest:
                resume = datetime.fromisoformat(latest) + timedelta(minutes=STEP_MINUTES)
                log.info("resuming the data clock from stored history at %s",
                         resume.isoformat())
                return resume
            # A reachable API with an empty reading table is a genuinely fresh
            # deployment, and there is nothing to rewind into.
            log.info("no stored readings; starting the data clock at now")
            return datetime.now(UTC).replace(second=0, microsecond=0)

        if attempt < attempts:
            time.sleep(delay_s)

    raise ResumePointUnavailable(
        f"{url} unreachable after {attempts} attempts ({last_error}). Refusing to "
        f"start: guessing the resume point rewinds the data clock and publishes "
        f"readings that are silently discarded as duplicates."
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s  %(message)s"
    )
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="data_start", default=None,
                        help="ISO timestamp to start the data clock (default: resume from stored history)")
    parser.add_argument("--max-readings", type=int, default=None,
                        help="stop after publishing this many (for smoke tests)")
    args = parser.parse_args(argv)

    settings = get_settings()
    buildings = load_buildings()
    try:
        start = (
            datetime.fromisoformat(args.data_start)
            if args.data_start
            else resume_point()
        )
    except ResumePointUnavailable as exc:
        log.error("%s", exc)
        return 1

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
