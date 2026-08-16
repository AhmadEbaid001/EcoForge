"""Replay of a real open smart-meter dataset onto the portfolio.

The proposal (Section 2.2) commits to replaying a public dataset alongside the
synthetic nodes, so that the demonstration rests partly on measured behaviour rather
than entirely on a generator the team wrote. This is that path.

Default source is the UCI *Individual Household Electric Power Consumption* dataset:
2{,}075{,}259 minute-resolution readings from one French household, December 2006 to
November 2010. Semicolon-separated, with `?` for missing values, which is itself
useful - real meter data has gaps, and a pipeline that only ever sees clean synthetic
data has not been tested.

**Amplitude is rescaled, shape is not.** A single household draws a few kW; a
government building draws hundreds. Replaying the raw values would put a building's
consumption two orders of magnitude below its profile and quietly wreck every savings
estimate derived from it. The series is therefore scaled so its mean matches the
target building's mean load, which preserves exactly what the dataset is here for -
the statistical texture of real demand, its spikes, its plateaux, its missing runs -
while keeping the energy total consistent with the portfolio.

    python -m gemp.sim.replay --file data/household_power_consumption.txt --building b001
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import signal
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import paho.mqtt.client as mqtt

from gemp.config import get_settings
from gemp.domain.models import Building
from gemp.domain.portfolio import load_buildings
from gemp.paths import data_dir

log = logging.getLogger("gemp.sim.replay")

HOURS_PER_YEAR = 8760.0
DEFAULT_FILENAME = "household_power_consumption.txt"

# UCI archive. Downloading is a separate, explicit step - see --help.
UCI_URL = (
    "https://archive.ics.uci.edu/static/public/235/"
    "individual+household+electric+power+consumption.zip"
)


@dataclass
class ReplaySeries:
    """A parsed dataset: timestamps and load in kW, gaps already dropped."""

    timestamps: list[datetime]
    kw: list[float]
    missing: int

    def __len__(self) -> int:
        return len(self.timestamps)

    @property
    def mean_kw(self) -> float:
        return sum(self.kw) / len(self.kw) if self.kw else 0.0


def dataset_path(explicit: Path | None = None) -> Path:
    return explicit or (data_dir() / DEFAULT_FILENAME)


def load_uci(path: Path, limit: int | None = None) -> ReplaySeries:
    """Parse the UCI household file.

    Columns: Date;Time;Global_active_power;Global_reactive_power;Voltage;...
    Date is dd/mm/yyyy, power is in kilowatts, `?` marks a missing reading.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Download the UCI dataset and place it there, or pass "
            f"--file. Source: {UCI_URL}"
        )

    timestamps: list[datetime] = []
    kw: list[float] = []
    missing = 0

    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh, delimiter=";")
        for row in reader:
            raw = (row.get("Global_active_power") or "").strip()
            if raw in ("", "?"):
                missing += 1
                continue
            try:
                value = float(raw)
                stamp = datetime.strptime(
                    f"{row['Date']} {row['Time']}", "%d/%m/%Y %H:%M:%S"
                ).replace(tzinfo=UTC)
            except (ValueError, KeyError):
                missing += 1
                continue

            timestamps.append(stamp)
            kw.append(value)
            if limit and len(kw) >= limit:
                break

    if not kw:
        raise ValueError(f"{path} contained no usable readings")

    return ReplaySeries(timestamps=timestamps, kw=kw, missing=missing)


def rescale_to_building(series: ReplaySeries, building: Building) -> list[float]:
    """Scale the dataset so its mean matches this building's mean load.

    Preserves shape, variance ratio and every gap; changes only amplitude. Without
    this a government building would appear to consume like one French household.
    """
    target_mean_kw = building.annual_kwh / HOURS_PER_YEAR
    source_mean = series.mean_kw
    if source_mean <= 0:
        raise ValueError("dataset mean is zero; cannot rescale")
    factor = target_mean_kw / source_mean
    return [value * factor for value in series.kw]


class ReplayFeed:
    """Publishes a replayed dataset onto the same MQTT topic as the simulator.

    Downstream components cannot tell a replayed reading from a simulated one or from
    a physical meter, which is the entire point of the topic being the hardware seam.
    Only `source` differs, so the origin stays auditable.
    """

    def __init__(self, building: Building, settings, step_minutes: int = 15):
        self.building = building
        self.settings = settings
        self.step = timedelta(minutes=step_minutes)
        self.published = 0
        self.running = True

        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"gemp-replay-{building.id}",
            clean_session=True,
        )
        self.client.username_pw_set(
            settings.mqtt_user, settings.mqtt_password.get_secret_value()
        )

    def connect(self) -> None:
        self.client.connect(self.settings.mqtt_host, self.settings.mqtt_port, keepalive=60)
        self.client.loop_start()

    def stop(self, *_args) -> None:
        self.running = False

    def run(self, values: list[float], data_start: datetime,
            max_readings: int | None = None) -> int:
        """Emit the replayed series on the accelerated data clock."""
        data_now = data_start
        wall_start = time.monotonic()
        index = 0

        log.info(
            "replaying %d readings onto %s at %dx",
            len(values), self.building.code, self.settings.sim_speed,
        )

        while self.running and index < len(values):
            elapsed = time.monotonic() - wall_start
            target = data_start + timedelta(seconds=elapsed * self.settings.sim_speed)

            while data_now <= target and index < len(values) and self.running:
                self._publish(data_now, values[index])
                data_now += self.step
                index += 1
                if max_readings is not None and self.published >= max_readings:
                    self.running = False

            time.sleep(min(self.settings.sim_interval_s, 1.0))

        self.client.loop_stop()
        self.client.disconnect()
        return self.published

    def _publish(self, ts: datetime, kw: float) -> None:
        payload = json.dumps({
            "building_id": self.building.id,
            "ts": ts.isoformat(timespec="seconds"),
            "kw": round(max(float(kw), 0.0), 4),
            "source": "replay",
        })
        self.client.publish(
            f"{self.settings.mqtt_topic_prefix}/{self.building.id}", payload, qos=1
        )
        self.published += 1


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s  %(message)s"
    )
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--file", type=Path, default=None,
                        help=f"dataset file (default: data/{DEFAULT_FILENAME})")
    parser.add_argument("--building", default=None,
                        help="target building id (default: the first in the portfolio)")
    parser.add_argument("--limit", type=int, default=200_000,
                        help="stop parsing after this many readings")
    parser.add_argument("--step-minutes", type=int, default=15)
    parser.add_argument("--from", dest="data_start", default=None,
                        help="ISO timestamp to start the data clock")
    parser.add_argument("--max-readings", type=int, default=None)
    parser.add_argument("--stats-only", action="store_true",
                        help="parse and report, publish nothing")
    args = parser.parse_args(argv)

    buildings = {b.id: b for b in load_buildings()}
    if args.building and args.building not in buildings:
        print(f"FAIL  no building {args.building!r} in the portfolio", file=sys.stderr)
        return 1
    building = buildings[args.building] if args.building else next(iter(buildings.values()))

    try:
        series = load_uci(dataset_path(args.file), args.limit)
    except (FileNotFoundError, ValueError) as exc:
        print(f"FAIL  {exc}", file=sys.stderr)
        return 1

    values = rescale_to_building(series, building)

    print(f"  dataset      {len(series):,} readings, {series.missing:,} missing/skipped")
    print(f"  span         {series.timestamps[0].date()} to {series.timestamps[-1].date()}")
    print(f"  source mean  {series.mean_kw:.3f} kW")
    print(f"  target       {building.code}, {building.annual_kwh:,.0f} kWh/yr "
          f"({building.annual_kwh / HOURS_PER_YEAR:.1f} kW mean)")
    print(f"  scale factor {values[0] / series.kw[0]:.1f}x")

    if args.stats_only:
        return 0

    settings = get_settings()
    start = (
        datetime.fromisoformat(args.data_start)
        if args.data_start
        else datetime.now(UTC).replace(second=0, microsecond=0)
    )

    feed = ReplayFeed(building, settings, args.step_minutes)
    signal.signal(signal.SIGINT, feed.stop)
    signal.signal(signal.SIGTERM, feed.stop)

    try:
        feed.connect()
    except OSError as exc:
        print(f"FAIL  cannot reach the broker: {exc}", file=sys.stderr)
        return 1

    published = feed.run(values, start, args.max_readings)
    log.info("published %d replayed readings", published)
    return 0


if __name__ == "__main__":
    sys.exit(main())
