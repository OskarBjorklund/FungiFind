from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Self

import pytest

from fungifind.data_sources.smhi_mesan import (
    MesanGridPoint,
    MesanHourlyAnalysis,
    MesanPointAnalysis,
)
from fungifind.data_sources.smhi_mesan_archive import (
    CachedHttpRangeReader,
    HistoricalGridSelection,
    HistoricalMesanAsset,
    HistoricalMesanError,
    HistoricalMesanHour,
    MesanArchiveFeedClient,
    parse_grib_message_header,
)
from fungifind.data_sources.weather_history import (
    CoverageStatus,
    MesanHistoryArchive,
    backfill_mesan_history,
    get_weather_history_features,
    ingest_recent_mesan,
)

GRID = MesanGridPoint(59.418897, 18.129709)
REQUESTED = MesanGridPoint(59.412, 18.132)
GRID_ID = "grib2-section3-sha256:test-grid"


def _asset(timestamp: datetime) -> HistoricalMesanAsset:
    filename = f"MESAN_{timestamp:%Y%m%d%H%M}+000H00M"
    return HistoricalMesanAsset(
        timestamp=timestamp,
        href=f"https://archive.test/{filename}",
        filename=filename,
        updated_time=timestamp + timedelta(minutes=39),
    )


def _historical_hour(
    timestamp: datetime,
    *,
    grid: MesanGridPoint = GRID,
    precipitation: float = 1.0,
    temperature: float = 10.0,
) -> HistoricalMesanHour:
    asset = _asset(timestamp)
    return HistoricalMesanHour(
        requested_point=REQUESTED,
        grid_point=grid,
        time=timestamp,
        air_temperature_c=temperature,
        precipitation_1h_mm=precipitation,
        relative_humidity_percent=60.0,
        wind_speed_m_s=2.0,
        wind_u_m_s=2.0,
        wind_v_m_s=0.0,
        source_api="SMHI_GRID_ARCHIVE",
        source_product="SMHI_MESAN_GRIDPP",
        source_version="feed-9/grib2",
        source_url=asset.href,
        source_file=asset.filename,
        grid_identity=GRID_ID,
        sampling_method="nearest_grid_cell_center:no_interpolation",
        source_units={
            "air_temperature_raw": "K",
            "air_temperature_stored": "degrees_celsius",
            "precipitation_1h_raw": "mm",
            "precipitation_1h_stored": "mm",
            "relative_humidity_raw": "fraction",
            "relative_humidity_stored": "percent",
            "wind_u_raw": "m/s",
            "wind_v_raw": "m/s",
            "wind_speed_stored": "m/s",
        },
        reference_time=timestamp,
        created_time=asset.updated_time,
    )


class _Feed:
    def __init__(self, assets: list[HistoricalMesanAsset]) -> None:
        self.assets = tuple(assets)

    def list_period(
        self, start_time: datetime, end_time: datetime
    ) -> tuple[tuple[HistoricalMesanAsset, ...], int]:
        return (
            tuple(asset for asset in self.assets if start_time <= asset.timestamp <= end_time),
            0,
        )


class _Layout:
    grid_identity = GRID_ID


class _Reader:
    def __init__(
        self,
        *,
        grid: MesanGridPoint = GRID,
        precipitation: float = 1.0,
        temperature: float = 10.0,
        error: Exception | None = None,
    ) -> None:
        self.grid = grid
        self.precipitation = precipitation
        self.temperature = temperature
        self.error = error

    def sample_assets(
        self,
        assets: list[HistoricalMesanAsset],
        *,
        latitude: float,
        longitude: float,
        max_workers: int = 8,
    ) -> tuple[_Layout, HistoricalGridSelection, tuple[HistoricalMesanHour, ...]]:
        if self.error is not None:
            raise self.error
        selection = HistoricalGridSelection(
            requested_point=MesanGridPoint(latitude, longitude),
            grid_point=self.grid,
            row=10,
            column=20,
            packed_value_index=30,
            grid_identity=GRID_ID,
            crs_wkt="PROJCS[test]",
            grid_resolution_m=(2500.0, 2500.0),
        )
        return (
            _Layout(),
            selection,
            tuple(
                _historical_hour(
                    asset.timestamp,
                    grid=self.grid,
                    precipitation=self.precipitation,
                    temperature=self.temperature,
                )
                for asset in assets
            ),
        )


class _LiveClient:
    def __init__(self, start: datetime, count: int) -> None:
        units = {
            "air_temperature": "Cel",
            "precipitation_amount_last_1_hours": "mm",
            "relative_humidity": "percent",
            "wind_speed": "m/s",
        }
        self.analysis = MesanPointAnalysis(
            requested_point=REQUESTED,
            grid_point=GRID,
            reference_time=start + timedelta(hours=count - 1),
            created_time=start + timedelta(hours=count - 1, minutes=39),
            timesteps=tuple(
                MesanHourlyAnalysis(
                    time=start + timedelta(hours=index),
                    grid_point=GRID,
                    air_temperature_c=10.0,
                    precipitation_1h_mm=1.0,
                    relative_humidity_percent=60.0,
                    wind_speed_m_s=2.0,
                    values={},
                    units=units,
                )
                for index in range(count)
            ),
            parameters={},
        )

    def get_point_analysis(self, latitude: float, longitude: float) -> MesanPointAnalysis:
        return self.analysis


def test_30_day_backfill_is_idempotent_preserves_live_and_has_full_coverage(
    tmp_path: Path,
) -> None:
    database = tmp_path / "history.sqlite"
    start = datetime(2026, 7, 28, 12, tzinfo=UTC)
    target = start + timedelta(hours=719)
    live_start = target - timedelta(hours=23)
    ingest_recent_mesan(
        REQUESTED.latitude,
        REQUESTED.longitude,
        archive_path=database,
        client=_LiveClient(live_start, 24),
    )
    assets = [_asset(start + timedelta(hours=index)) for index in range(720)]

    first = backfill_mesan_history(
        REQUESTED.latitude,
        REQUESTED.longitude,
        days=30,
        archive_path=database,
        cache_path=tmp_path / "cache",
        end_time=target,
        feed_client=_Feed(assets),
        point_reader=_Reader(),
    )
    duplicate = backfill_mesan_history(
        REQUESTED.latitude,
        REQUESTED.longitude,
        days=30,
        archive_path=database,
        cache_path=tmp_path / "cache",
        end_time=target,
        feed_client=_Feed(assets),
        point_reader=_Reader(),
    )
    features = get_weather_history_features(GRID, target, archive_path=database)

    assert first.statistics.discovered == 720
    assert first.statistics.parsed == 720
    assert first.statistics.inserted == 696
    assert first.statistics.already_present == 24
    assert len(first.overlap_differences) == 24
    assert duplicate.statistics.inserted == 0
    assert duplicate.statistics.already_present == 720
    assert features.rain_30d_mm.status is CoverageStatus.FULL
    assert features.rain_30d_mm.value == pytest.approx(720.0)
    assert features.temp_mean_14d_c.value == pytest.approx(10.0)
    assert features.relative_humidity_mean_7d.value == pytest.approx(60.0)

    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        historical = connection.execute(
            "SELECT * FROM mesan_hourly_analysis WHERE timestamp_utc=?",
            (start.isoformat().replace("+00:00", "Z"),),
        ).fetchone()
        live = connection.execute(
            "SELECT * FROM mesan_hourly_analysis WHERE timestamp_utc=?",
            (target.isoformat().replace("+00:00", "Z"),),
        ).fetchone()
    assert historical["source_product"] == "SMHI_MESAN_GRIDPP"
    assert historical["source_file"].startswith("MESAN_")
    assert historical["grid_identity"] == GRID_ID
    assert historical["sampling_method"] == "nearest_grid_cell_center:no_interpolation"
    assert '"relative_humidity_raw":"fraction"' in historical["source_units_json"]
    assert live["source_product"] == "SMHI_MESAN2G"
    assert live["source_priority"] > historical["source_priority"]


def test_partial_archive_reports_unavailable_hours_and_partial_coverage(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 8, 24, 12, tzinfo=UTC)
    target = start + timedelta(hours=71)
    assets = [_asset(start + timedelta(hours=index)) for index in range(48)]
    database = tmp_path / "history.sqlite"

    result = backfill_mesan_history(
        REQUESTED.latitude,
        REQUESTED.longitude,
        days=3,
        archive_path=database,
        cache_path=tmp_path / "cache",
        end_time=target,
        feed_client=_Feed(assets),
        point_reader=_Reader(),
    )
    features = get_weather_history_features(GRID, target, archive_path=database)

    assert result.statistics.unavailable == 24
    assert result.statistics.inserted == 48
    assert features.rain_3d_mm.status is CoverageStatus.PARTIAL
    assert features.rain_3d_mm.available_hours == 48


def test_failed_or_missing_source_does_not_mutate_database(tmp_path: Path) -> None:
    database = tmp_path / "history.sqlite"
    start = datetime(2026, 8, 26, 0, tzinfo=UTC)
    ingest_recent_mesan(
        REQUESTED.latitude,
        REQUESTED.longitude,
        archive_path=database,
        client=_LiveClient(start, 1),
    )
    before = hashlib.sha256(database.read_bytes()).hexdigest()

    with pytest.raises(HistoricalMesanError, match="corrupt"):
        backfill_mesan_history(
            REQUESTED.latitude,
            REQUESTED.longitude,
            days=1,
            archive_path=database,
            cache_path=tmp_path / "cache",
            end_time=start,
            feed_client=_Feed([_asset(start)]),
            point_reader=_Reader(error=HistoricalMesanError("corrupt download")),
        )
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before

    with pytest.raises(Exception, match="No historical MESAN assets"):
        backfill_mesan_history(
            REQUESTED.latitude,
            REQUESTED.longitude,
            days=1,
            archive_path=database,
            cache_path=tmp_path / "cache",
            end_time=start,
            feed_client=_Feed([]),
            point_reader=_Reader(),
        )
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_different_historical_grids_are_stored_separately(tmp_path: Path) -> None:
    database = tmp_path / "history.sqlite"
    timestamp = datetime(2026, 8, 1, 0, tzinfo=UTC)
    second_grid = MesanGridPoint(59.42, 18.15)

    backfill_mesan_history(
        REQUESTED.latitude,
        REQUESTED.longitude,
        days=1,
        archive_path=database,
        cache_path=tmp_path / "cache",
        end_time=timestamp,
        feed_client=_Feed([_asset(timestamp)]),
        point_reader=_Reader(grid=GRID),
    )
    backfill_mesan_history(
        REQUESTED.latitude,
        REQUESTED.longitude,
        days=1,
        archive_path=database,
        cache_path=tmp_path / "cache",
        end_time=timestamp,
        feed_client=_Feed([_asset(timestamp)]),
        point_reader=_Reader(grid=second_grid),
    )

    assert set(MesanHistoryArchive(database).list_grid_points()) == {GRID, second_grid}


def test_live_priority_replaces_lower_priority_history(tmp_path: Path) -> None:
    database = tmp_path / "history.sqlite"
    timestamp = datetime(2026, 8, 26, 12, tzinfo=UTC)
    backfill_mesan_history(
        REQUESTED.latitude,
        REQUESTED.longitude,
        days=1,
        archive_path=database,
        cache_path=tmp_path / "cache",
        end_time=timestamp,
        feed_client=_Feed([_asset(timestamp)]),
        point_reader=_Reader(temperature=8.0),
    )

    ingest_recent_mesan(
        REQUESTED.latitude,
        REQUESTED.longitude,
        archive_path=database,
        client=_LiveClient(timestamp, 1),
    )

    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT source_product, air_temperature_c FROM mesan_hourly_analysis"
        ).fetchone()
    assert row == ("SMHI_MESAN2G", 10.0)


def test_atom_feed_parser_discovers_official_hourly_assets() -> None:
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title> 2026-08-26 12:00 UTC</title>
        <link rel="enclosure" href="https://archive.test/MESAN_202608261200+000H00M" />
        <updated>2026-08-26T12:39:10+0000</updated>
      </entry>
    </feed>"""

    class Response:
        status = 200

        def read(self) -> bytes:
            return xml

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    assets = MesanArchiveFeedClient(
        "https://archive.test/feed/9",
        opener=lambda request, timeout: Response(),
    ).list_day(datetime(2026, 8, 26, tzinfo=UTC).date())

    assert len(assets) == 1
    assert assets[0].timestamp == datetime(2026, 8, 26, 12, tzinfo=UTC)
    assert assets[0].filename == "MESAN_202608261200+000H00M"


def _pilot_header_prefix() -> bytes:
    # Sections 0--6 from one real feed-9 temperature message, followed by the
    # section-7 header. The packed field itself is deliberately omitted.
    return bytes.fromhex(
        "47524942ffff000200000000002e712f"
        "00000015010052000015000107ea081a0c00000002"
        "000000510300000f7ad10000001e06ffffffffffffffffffffffffffffff"
        "000003b50000042d02ffd10000043f083803c5e1a000e4e1c0002625a0"
        "002625a0004003c5e1a003c5e1a0855d4a8000000000"
        "000000220400000000000000ffa50000000100000000670000000002ffffffffffff"
        "0000001505000f7ad10000438843ec801300001800"
        "0000000606ff"
        "002e707807"
    )


def test_grib_header_parser_preserves_grid_time_units_and_parameter_identity() -> None:
    metadata = parse_grib_message_header(_pilot_header_prefix())

    assert metadata.parameter_key == (0, 0, 103, 2, 0)
    assert metadata.reference_time == datetime(2026, 8, 26, 12, tzinfo=UTC)
    assert metadata.valid_time == metadata.reference_time
    assert (metadata.width, metadata.height) == (949, 1069)
    assert metadata.scanning_mode == 0x40
    assert metadata.bits_per_value == 24
    assert metadata.grid_identity.startswith("grib2-section3-sha256:")


class _RangeResponse:
    def __init__(self, raw: bytes, *, content_range: str) -> None:
        self.status = 206
        self.headers = {"Content-Range": content_range, "Content-Length": str(len(raw))}
        self.raw = raw

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            result, self.raw = self.raw, b""
            return result
        result, self.raw = self.raw[:size], self.raw[size:]
        return result

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def test_corrupt_streaming_range_download_leaves_no_file_or_part(tmp_path: Path) -> None:
    data = b"0123456789"

    def opener(request: Any, *, timeout: float) -> _RangeResponse:
        if request.get_method() == "HEAD":
            response = _RangeResponse(b"", content_range="bytes 0-0/10")
            response.status = 200
            response.headers["Content-Length"] = "10"
            return response
        return _RangeResponse(data[:5], content_range="bytes 0-9/10")

    reader = CachedHttpRangeReader(tmp_path / "cache", opener=opener)
    target = tmp_path / "template.grib2"

    with pytest.raises(HistoricalMesanError, match="Incomplete GRIB template"):
        reader.download("https://archive.test/file", 0, 10, target)
    assert not target.exists()
    assert not target.with_suffix(".grib2.part").exists()
