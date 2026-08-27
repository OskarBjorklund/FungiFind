from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fungifind.data_sources.smhi_mesan import (
    MesanClientError,
    MesanGridPoint,
    MesanHourlyAnalysis,
    MesanPointAnalysis,
)
from fungifind.data_sources.weather_history import (
    CoverageStatus,
    MesanHistoryArchive,
    get_weather_history_features,
    ingest_recent_mesan,
)

GRID_POINT = MesanGridPoint(59.418897, 18.129709)
REQUESTED_POINT = MesanGridPoint(59.412, 18.132)


def _analysis(
    start: datetime,
    count: int,
    *,
    missing_indexes: frozenset[int] = frozenset(),
    precipitation: float = 1.0,
) -> MesanPointAnalysis:
    units = {
        "air_temperature": "Cel",
        "precipitation_amount_last_1_hours": "mm",
        "relative_humidity": "percent",
        "wind_speed": "m/s",
    }
    timesteps = []
    for index in range(count):
        missing = index in missing_indexes
        timestamp = start + timedelta(hours=index)
        timesteps.append(
            MesanHourlyAnalysis(
                time=timestamp,
                grid_point=GRID_POINT,
                air_temperature_c=None if missing else float(index),
                precipitation_1h_mm=None if missing else precipitation,
                relative_humidity_percent=None if missing else 50.0,
                wind_speed_m_s=None if missing else 2.0,
                values={},
                units=units,
            )
        )
    return MesanPointAnalysis(
        requested_point=REQUESTED_POINT,
        grid_point=GRID_POINT,
        reference_time=start + timedelta(hours=count - 1),
        created_time=start + timedelta(hours=count - 1, minutes=30),
        timesteps=tuple(timesteps),
        parameters={},
    )


class _Client:
    def __init__(self, analyses: list[MesanPointAnalysis]) -> None:
        self.analyses = analyses

    def get_point_analysis(self, latitude: float, longitude: float) -> MesanPointAnalysis:
        return self.analyses.pop(0)


def test_duplicate_and_overlapping_ingestion_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "history.sqlite"
    start = datetime(2026, 8, 26, 0, tzinfo=UTC)
    first = _analysis(start, 24)
    overlap = _analysis(start + timedelta(hours=12), 24)
    client = _Client([first, first, overlap])

    initial = ingest_recent_mesan(
        59.412, 18.132, archive_path=database, client=client
    )
    duplicate = ingest_recent_mesan(
        59.412, 18.132, archive_path=database, client=client
    )
    overlapping = ingest_recent_mesan(
        59.412, 18.132, archive_path=database, client=client
    )

    assert initial.statistics.inserted == 24
    assert initial.statistics.already_present == 0
    assert duplicate.statistics.inserted == 0
    assert duplicate.statistics.already_present == 24
    assert overlapping.statistics.inserted == 12
    assert overlapping.statistics.already_present == 12
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM mesan_hourly_analysis"
        ).fetchone()[0] == 36
        primary_key = connection.execute(
            "PRAGMA table_info(mesan_hourly_analysis)"
        ).fetchall()
    assert [row[1] for row in primary_key if row[5] > 0] == [
        "grid_latitude",
        "grid_longitude",
        "timestamp_utc",
    ]


def test_invalid_hours_are_skipped_and_become_missing_coverage(tmp_path: Path) -> None:
    database = tmp_path / "history.sqlite"
    start = datetime(2026, 8, 26, 12, tzinfo=UTC)
    result = ingest_recent_mesan(
        59.412,
        18.132,
        archive_path=database,
        client=_Client([_analysis(start, 24, missing_indexes=frozenset({5}))]),
    )
    features = get_weather_history_features(
        GRID_POINT,
        start + timedelta(hours=23),
        archive_path=database,
    )

    assert result.statistics.fetched == 24
    assert result.statistics.inserted == 23
    assert result.statistics.invalid == 1
    assert features.rain_1d_mm.status is CoverageStatus.PARTIAL
    assert features.rain_1d_mm.available_hours == 23
    assert features.rain_1d_mm.expected_hours == 24
    assert features.rain_1d_mm.coverage_fraction == pytest.approx(23 / 24)
    assert features.rain_1d_mm.value is None


def test_complete_windows_calculate_sum_means_and_partial_coverage(tmp_path: Path) -> None:
    database = tmp_path / "history.sqlite"
    start = datetime(2026, 8, 24, 12, tzinfo=UTC)
    ingest_recent_mesan(
        59.412,
        18.132,
        archive_path=database,
        client=_Client([_analysis(start, 72, precipitation=0.5)]),
    )
    target = start + timedelta(hours=71)
    features = get_weather_history_features(
        GRID_POINT,
        target,
        archive_path=database,
    )

    assert features.rain_1d_mm.status is CoverageStatus.FULL
    assert features.rain_1d_mm.value == pytest.approx(12.0)
    assert features.rain_3d_mm.status is CoverageStatus.FULL
    assert features.rain_3d_mm.value == pytest.approx(36.0)
    assert features.rain_7d_mm.status is CoverageStatus.PARTIAL
    assert features.rain_7d_mm.available_hours == 72
    assert features.rain_7d_mm.coverage_fraction == pytest.approx(72 / 168)
    assert features.rain_7d_mm.value is None
    assert features.temp_mean_3d_c.status is CoverageStatus.FULL
    assert features.temp_mean_3d_c.value == pytest.approx(35.5)
    assert features.relative_humidity_mean_3d.value == pytest.approx(50.0)
    assert features.relative_humidity_mean_7d.status is CoverageStatus.PARTIAL
    assert features.days_since_significant_rain.status is CoverageStatus.THRESHOLD_REQUIRED
    assert features.dry_spell_length.status is CoverageStatus.THRESHOLD_REQUIRED


def test_explicit_rain_threshold_enables_bounded_dry_period(tmp_path: Path) -> None:
    database = tmp_path / "history.sqlite"
    start = datetime(2026, 8, 26, 0, tzinfo=UTC)
    analysis = _analysis(start, 24, precipitation=0.0)
    records = list(analysis.timesteps)
    records[18] = MesanHourlyAnalysis(
        time=records[18].time,
        grid_point=GRID_POINT,
        air_temperature_c=18.0,
        precipitation_1h_mm=5.0,
        relative_humidity_percent=50.0,
        wind_speed_m_s=2.0,
        values={},
        units=records[18].units,
    )
    analysis = MesanPointAnalysis(
        requested_point=analysis.requested_point,
        grid_point=analysis.grid_point,
        reference_time=analysis.reference_time,
        created_time=analysis.created_time,
        timesteps=tuple(records),
        parameters={},
    )
    ingest_recent_mesan(
        59.412,
        18.132,
        archive_path=database,
        client=_Client([analysis]),
    )

    features = get_weather_history_features(
        GRID_POINT,
        records[-1].time,
        archive_path=database,
        significant_rain_threshold_mm=4.0,
    )

    assert features.days_since_significant_rain.status is CoverageStatus.FULL
    assert features.days_since_significant_rain.value == pytest.approx(5 / 24)
    assert features.dry_spell_length.value == pytest.approx(5 / 24)


def test_failed_fetch_leaves_existing_database_byte_for_byte_unchanged(
    tmp_path: Path,
) -> None:
    database = tmp_path / "history.sqlite"
    start = datetime(2026, 8, 26, 0, tzinfo=UTC)
    ingest_recent_mesan(
        59.412,
        18.132,
        archive_path=database,
        client=_Client([_analysis(start, 1)]),
    )
    before_hash = hashlib.sha256(database.read_bytes()).hexdigest()

    class FailingClient:
        def get_point_analysis(
            self, latitude: float, longitude: float
        ) -> MesanPointAnalysis:
            raise MesanClientError("malformed response")

    with pytest.raises(MesanClientError, match="malformed"):
        ingest_recent_mesan(
            59.412,
            18.132,
            archive_path=database,
            client=FailingClient(),
        )

    assert hashlib.sha256(database.read_bytes()).hexdigest() == before_hash


def test_nearby_locations_reuse_the_same_stored_grid_point(tmp_path: Path) -> None:
    database = tmp_path / "history.sqlite"
    start = datetime(2026, 8, 26, 0, tzinfo=UTC)
    ingest_recent_mesan(
        59.412,
        18.132,
        archive_path=database,
        client=_Client([_analysis(start, 1)]),
    )

    selected = MesanHistoryArchive(database).find_nearest_grid_point(59.413, 18.131)

    assert selected == GRID_POINT

