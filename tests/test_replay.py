"""Dataset replay: parse a real-format file, rescale amplitude, keep shape.

Uses a small inline fixture in the UCI file format rather than the 20 MB dataset, so
the suite stays fast and runs with no download. The format quirks that matter are all
represented: semicolon separation, dd/mm/yyyy dates, and `?` for missing readings.
"""

from __future__ import annotations

import statistics

import pytest
from tests.conftest import make_building

from gemp.sim.replay import HOURS_PER_YEAR, load_uci, rescale_to_building

HEADER = (
    "Date;Time;Global_active_power;Global_reactive_power;Voltage;"
    "Global_intensity;Sub_metering_1;Sub_metering_2;Sub_metering_3"
)


def write_dataset(path, rows: list[str]) -> None:
    path.write_text("\n".join([HEADER, *rows]) + "\n", encoding="utf-8")


def sample_rows(count: int = 60) -> list[str]:
    rows = []
    for i in range(count):
        minute = i % 60
        hour = (i // 60) % 24
        power = 1.0 + (i % 10) * 0.35          # deterministic, varied
        rows.append(
            f"16/12/2006;{hour:02d}:{minute:02d}:00;{power:.3f};"
            f"0.418;234.840;18.400;0.000;1.000;17.000"
        )
    return rows


@pytest.fixture
def dataset(tmp_path):
    path = tmp_path / "household_power_consumption.txt"
    write_dataset(path, sample_rows())
    return path


# --- parsing ----------------------------------------------------------------


def test_parses_the_uci_format(dataset):
    series = load_uci(dataset)
    assert len(series) == 60
    assert series.missing == 0
    assert series.timestamps[0].day == 16
    assert series.timestamps[0].month == 12
    assert series.timestamps[0].year == 2006


def test_missing_readings_are_skipped_and_counted(tmp_path):
    """`?` marks a gap in the real file. Real meter data has gaps, and a pipeline
    that has only ever seen clean synthetic data has not been tested."""
    path = tmp_path / "gaps.txt"
    write_dataset(path, [
        "16/12/2006;17:24:00;4.216;0.418;234.840;18.400;0.000;1.000;17.000",
        "16/12/2006;17:25:00;?;?;?;?;;;",
        "16/12/2006;17:26:00;5.360;0.436;233.630;23.000;0.000;1.000;16.000",
    ])

    series = load_uci(path)
    assert len(series) == 2
    assert series.missing == 1


def test_malformed_rows_do_not_abort_the_parse(tmp_path):
    path = tmp_path / "malformed.txt"
    write_dataset(path, [
        "16/12/2006;17:24:00;4.216;0.418;234.840;18.400;0.000;1.000;17.000",
        "not-a-date;nonsense;abc;;;;;;",
        "16/12/2006;17:26:00;5.360;0.436;233.630;23.000;0.000;1.000;16.000",
    ])

    series = load_uci(path)
    assert len(series) == 2
    assert series.missing == 1


def test_limit_stops_early(dataset):
    assert len(load_uci(dataset, limit=10)) == 10


def test_missing_file_names_the_source(tmp_path):
    with pytest.raises(FileNotFoundError, match="archive.ics.uci.edu"):
        load_uci(tmp_path / "absent.txt")


def test_empty_dataset_is_rejected(tmp_path):
    path = tmp_path / "empty.txt"
    write_dataset(path, ["16/12/2006;17:24:00;?;?;?;?;;;"])
    with pytest.raises(ValueError, match="no usable readings"):
        load_uci(path)


# --- rescaling --------------------------------------------------------------


def test_rescaled_mean_matches_the_building(dataset):
    """A household draws a few kW; a government building draws hundreds. Replaying
    raw values would put consumption two orders of magnitude below the profile and
    wreck every savings estimate derived from it."""
    series = load_uci(dataset)
    building = make_building(annual_kwh=876_000.0)      # exactly 100 kW mean

    values = rescale_to_building(series, building)

    assert statistics.mean(values) == pytest.approx(
        building.annual_kwh / HOURS_PER_YEAR, rel=1e-9
    )
    assert statistics.mean(values) == pytest.approx(100.0, rel=1e-9)


def test_rescaling_preserves_shape(dataset):
    """Amplitude changes, texture does not - the texture is the whole reason for
    replaying measured data rather than generating it."""
    series = load_uci(dataset)
    building = make_building(annual_kwh=876_000.0)

    values = rescale_to_building(series, building)

    # Ratios between consecutive samples are unchanged under a scalar multiple.
    original = [b / a for a, b in zip(series.kw[:-1], series.kw[1:], strict=True) if a > 0]
    scaled = [b / a for a, b in zip(values[:-1], values[1:], strict=True) if a > 0]
    assert scaled == pytest.approx(original, rel=1e-9)

    # Coefficient of variation is scale-invariant, so it must survive exactly.
    def cv(xs):
        return statistics.pstdev(xs) / statistics.mean(xs)

    assert cv(values) == pytest.approx(cv(series.kw), rel=1e-9)


def test_rescaling_is_proportional_to_building_size(dataset):
    series = load_uci(dataset)
    small = rescale_to_building(series, make_building(id="s", annual_kwh=100_000.0))
    large = rescale_to_building(series, make_building(id="l", annual_kwh=400_000.0))

    assert statistics.mean(large) == pytest.approx(4 * statistics.mean(small), rel=1e-9)
