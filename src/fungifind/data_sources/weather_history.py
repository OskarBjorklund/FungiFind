"""Persistent MESAN history and coverage-aware weather aggregates."""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from threading import RLock
from types import MappingProxyType
from typing import Protocol

from fungifind.data_sources.smhi_mesan import (
    MESAN_SOURCE_NAME,
    MESAN_SOURCE_VERSION,
    MesanGridPoint,
    MesanHourlyAnalysis,
    MesanPointAnalysis,
    SmhiMesanPointClient,
    format_utc_datetime,
    parse_utc_datetime,
)
from fungifind.data_sources.smhi_mesan_archive import (
    DEFAULT_MESAN_ARCHIVE_CACHE,
    HISTORICAL_SOURCE_PRODUCT,
    HistoricalGridSelection,
    HistoricalMesanHour,
    MesanArchiveFeedClient,
    MesanHistoricalGribPointReader,
    historical_units_json,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MESAN_HISTORY_DATABASE = (
    PROJECT_ROOT / "src" / "data" / "weather" / "mesan_history.sqlite"
)
SCHEMA_VERSION = "2"
LIVE_SOURCE_PRIORITY = 100
HISTORICAL_SOURCE_PRIORITY = 50


class MesanHistoryError(RuntimeError):
    """Raised when the local history archive is missing or malformed."""


class CoverageStatus(str, Enum):
    FULL = "full"
    PARTIAL = "partial"
    INSUFFICIENT = "insufficient"
    THRESHOLD_REQUIRED = "threshold_required"
    UNBOUNDED = "unbounded"


@dataclass(frozen=True, slots=True)
class MesanArchiveRecord:
    grid_point: MesanGridPoint
    time: datetime
    air_temperature_c: float | None
    precipitation_1h_mm: float | None
    relative_humidity_percent: float | None
    wind_speed_m_s: float | None
    source_api: str
    source_product: str
    source_version: str
    source_url: str
    source_file: str
    grid_identity: str
    sampling_method: str
    requested_latitude: float | None
    requested_longitude: float | None
    source_units: Mapping[str, str]
    source_priority: int
    reference_time: datetime
    created_time: datetime


@dataclass(frozen=True, slots=True)
class IngestionStatistics:
    fetched: int
    inserted: int
    already_present: int
    invalid: int


@dataclass(frozen=True, slots=True)
class HistoricalBackfillStatistics:
    discovered: int
    parsed: int
    inserted: int
    already_present: int
    invalid: int
    unavailable: int


@dataclass(frozen=True, slots=True)
class MesanOverlapDifference:
    time: datetime
    retained_source_product: str
    historical_source_product: str
    air_temperature_difference_c: float
    precipitation_1h_difference_mm: float
    relative_humidity_difference_percent: float
    wind_speed_difference_m_s: float


@dataclass(frozen=True, slots=True)
class MesanHistoricalBackfillResult:
    requested_point: MesanGridPoint
    grid_point: MesanGridPoint
    requested_start_time: datetime
    requested_end_time: datetime
    statistics: HistoricalBackfillStatistics
    overlap_differences: tuple[MesanOverlapDifference, ...]
    database_path: str
    cache_path: str
    grid_identity: str
    grid_crs_wkt: str
    grid_resolution_m: tuple[float, float]
    sampling_method: str
    earliest_stored_time: datetime | None
    latest_stored_time: datetime | None
    precedence_policy: str


@dataclass(frozen=True, slots=True)
class MesanIngestionResult:
    requested_point: MesanGridPoint
    grid_point: MesanGridPoint
    statistics: IngestionStatistics
    database_path: str
    earliest_stored_time: datetime | None
    latest_stored_time: datetime | None


@dataclass(frozen=True, slots=True)
class WeatherAggregate:
    value: float | None
    unit: str
    expected_hours: int
    available_hours: int
    coverage_fraction: float
    status: CoverageStatus


@dataclass(frozen=True, slots=True)
class WeatherHistoryFeatures:
    grid_point: MesanGridPoint
    target_time: datetime
    rain_1d_mm: WeatherAggregate
    rain_3d_mm: WeatherAggregate
    rain_7d_mm: WeatherAggregate
    rain_14d_mm: WeatherAggregate
    rain_21d_mm: WeatherAggregate
    rain_30d_mm: WeatherAggregate
    temp_mean_3d_c: WeatherAggregate
    temp_mean_7d_c: WeatherAggregate
    temp_mean_14d_c: WeatherAggregate
    relative_humidity_mean_3d: WeatherAggregate
    relative_humidity_mean_7d: WeatherAggregate
    days_since_significant_rain: WeatherAggregate
    dry_spell_length: WeatherAggregate
    significant_rain_threshold_mm: float | None

    def aggregates(self) -> tuple[tuple[str, WeatherAggregate], ...]:
        return tuple(
            (item.name, getattr(self, item.name))
            for item in fields(self)
            if isinstance(getattr(self, item.name), WeatherAggregate)
        )


class MesanPointClientProtocol(Protocol):
    def get_point_analysis(
        self, latitude: float, longitude: float
    ) -> MesanPointAnalysis: ...


class MesanArchiveFeedProtocol(Protocol):
    def list_period(
        self, start_time: datetime, end_time: datetime
    ) -> tuple[tuple[object, ...], int]: ...


class MesanHistoricalPointReaderProtocol(Protocol):
    def sample_assets(
        self,
        assets: Sequence[object],
        *,
        latitude: float,
        longitude: float,
        max_workers: int = 8,
    ) -> tuple[object, HistoricalGridSelection, tuple[HistoricalMesanHour, ...]]: ...


def _normalize_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Weather-history times must have a timezone")
    normalized = value.astimezone(UTC)
    if normalized.minute or normalized.second or normalized.microsecond:
        raise ValueError("Weather-history target time must be aligned to an exact hour")
    return normalized


def _record_is_valid(record: MesanHourlyAnalysis) -> bool:
    return (
        record.is_complete
        and record.time.minute == 0
        and record.time.second == 0
        and record.time.microsecond == 0
    )


class MesanHistoryArchive:
    """SQLite archive keyed by snapped MESAN latitude/longitude and UTC hour."""

    def __init__(self, path: str | Path = DEFAULT_MESAN_HISTORY_DATABASE) -> None:
        self.path = Path(path).resolve()
        self._reader_lock = RLock()
        self._reader: sqlite3.Connection | None = None

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS archive_metadata (
                    key TEXT NOT NULL PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mesan_hourly_analysis (
                    grid_latitude REAL NOT NULL,
                    grid_longitude REAL NOT NULL,
                    timestamp_utc TEXT NOT NULL,
                    air_temperature_c REAL,
                    precipitation_1h_mm REAL CHECK (
                        precipitation_1h_mm IS NULL OR precipitation_1h_mm >= 0
                    ),
                    relative_humidity_percent REAL CHECK (
                        relative_humidity_percent IS NULL
                        OR relative_humidity_percent BETWEEN 0 AND 100
                    ),
                    wind_speed_m_s REAL CHECK (
                        wind_speed_m_s IS NULL OR wind_speed_m_s >= 0
                    ),
                    source_api TEXT NOT NULL,
                    source_product TEXT NOT NULL,
                    source_version TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    source_file TEXT NOT NULL,
                    grid_identity TEXT NOT NULL,
                    sampling_method TEXT NOT NULL,
                    requested_latitude REAL,
                    requested_longitude REAL,
                    source_units_json TEXT NOT NULL,
                    source_priority INTEGER NOT NULL,
                    reference_time_utc TEXT NOT NULL,
                    created_time_utc TEXT NOT NULL,
                    inserted_at_utc TEXT NOT NULL,
                    PRIMARY KEY (grid_latitude, grid_longitude, timestamp_utc)
                );
                CREATE INDEX IF NOT EXISTS idx_mesan_hourly_timestamp
                    ON mesan_hourly_analysis(timestamp_utc);
                """
            )
            connection.execute(
                "INSERT OR REPLACE INTO archive_metadata(key, value) VALUES (?, ?)",
                ("schema_version", SCHEMA_VERSION),
            )
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(mesan_hourly_analysis)")
            }
            if "source_url" not in columns:
                connection.execute(
                    """
                    ALTER TABLE mesan_hourly_analysis
                    ADD COLUMN source_url TEXT NOT NULL DEFAULT
                    'https://opendata-download-metanalys.smhi.se/api/category/mesan2g/version/3'
                    """
                )
            migrations = {
                "source_product": (
                    "TEXT NOT NULL DEFAULT 'SMHI_MESAN2G'"
                ),
                "source_file": "TEXT NOT NULL DEFAULT ''",
                "grid_identity": "TEXT NOT NULL DEFAULT 'legacy-api-grid'",
                "sampling_method": (
                    "TEXT NOT NULL DEFAULT 'api_nearest_grid_point:no_interpolation'"
                ),
                "requested_latitude": "REAL",
                "requested_longitude": "REAL",
                "source_units_json": "TEXT NOT NULL DEFAULT '{}'",
                "source_priority": f"INTEGER NOT NULL DEFAULT {LIVE_SOURCE_PRIORITY}",
            }
            for name, definition in migrations.items():
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE mesan_hourly_analysis ADD COLUMN {name} {definition}"
                    )

    @contextmanager
    def _existing_connection(self):
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        with self._reader_lock:
            if self._reader is None:
                uri = self.path.as_uri() + "?mode=ro"
                self._reader = sqlite3.connect(
                    uri,
                    uri=True,
                    check_same_thread=False,
                )
                self._reader.row_factory = sqlite3.Row
                self._reader.execute("PRAGMA query_only=ON")
            yield self._reader

    def close(self) -> None:
        """Close the reusable read-only archive connection."""

        with self._reader_lock:
            if self._reader is not None:
                self._reader.close()
            self._reader = None

    def insert_point_analysis(
        self, analysis: MesanPointAnalysis
    ) -> IngestionStatistics:
        valid = [record for record in analysis.timesteps if _record_is_valid(record)]
        invalid_count = len(analysis.timesteps) - len(valid)
        self.initialize()
        inserted_at = format_utc_datetime(datetime.now(UTC))
        rows = [
            (
                analysis.grid_point.normalized_latitude,
                analysis.grid_point.normalized_longitude,
                format_utc_datetime(record.time),
                record.air_temperature_c,
                record.precipitation_1h_mm,
                record.relative_humidity_percent,
                record.wind_speed_m_s,
                analysis.source_api,
                "SMHI_MESAN2G",
                analysis.source_version,
                analysis.source_url,
                "",
                (
                    "mesan2g-v3-point-grid:"
                    f"{analysis.grid_point.normalized_latitude:.6f},"
                    f"{analysis.grid_point.normalized_longitude:.6f}"
                ),
                "api_nearest_grid_point:no_interpolation",
                analysis.requested_point.latitude,
                analysis.requested_point.longitude,
                json.dumps(dict(record.units), sort_keys=True, separators=(",", ":")),
                LIVE_SOURCE_PRIORITY,
                format_utc_datetime(analysis.reference_time),
                format_utc_datetime(analysis.created_time),
                inserted_at,
            )
            for record in valid
        ]
        with sqlite3.connect(self.path) as connection:
            before = connection.total_changes
            connection.executemany(
                """
                INSERT INTO mesan_hourly_analysis (
                    grid_latitude, grid_longitude, timestamp_utc,
                    air_temperature_c, precipitation_1h_mm,
                    relative_humidity_percent, wind_speed_m_s,
                    source_api, source_product, source_version, source_url,
                    source_file, grid_identity, sampling_method,
                    requested_latitude, requested_longitude, source_units_json,
                    source_priority, reference_time_utc, created_time_utc,
                    inserted_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(grid_latitude, grid_longitude, timestamp_utc)
                DO UPDATE SET
                    air_temperature_c=excluded.air_temperature_c,
                    precipitation_1h_mm=excluded.precipitation_1h_mm,
                    relative_humidity_percent=excluded.relative_humidity_percent,
                    wind_speed_m_s=excluded.wind_speed_m_s,
                    source_api=excluded.source_api,
                    source_product=excluded.source_product,
                    source_version=excluded.source_version,
                    source_url=excluded.source_url,
                    source_file=excluded.source_file,
                    grid_identity=excluded.grid_identity,
                    sampling_method=excluded.sampling_method,
                    requested_latitude=excluded.requested_latitude,
                    requested_longitude=excluded.requested_longitude,
                    source_units_json=excluded.source_units_json,
                    source_priority=excluded.source_priority,
                    reference_time_utc=excluded.reference_time_utc,
                    created_time_utc=excluded.created_time_utc,
                    inserted_at_utc=excluded.inserted_at_utc
                WHERE excluded.source_priority > mesan_hourly_analysis.source_priority
                """,
                rows,
            )
            inserted = connection.total_changes - before
        return IngestionStatistics(
            fetched=len(analysis.timesteps),
            inserted=inserted,
            already_present=len(valid) - inserted,
            invalid=invalid_count,
        )

    def existing_records(
        self,
        grid_point: MesanGridPoint,
        timestamps: Sequence[datetime],
    ) -> Mapping[datetime, MesanArchiveRecord]:
        if not timestamps or not self.path.is_file():
            return MappingProxyType({})
        normalized = tuple(_normalize_time(value) for value in timestamps)
        placeholders = ",".join("?" for _ in normalized)
        with self._existing_connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM mesan_hourly_analysis
                WHERE grid_latitude=? AND grid_longitude=?
                  AND timestamp_utc IN ({placeholders})
                """,
                (
                    grid_point.normalized_latitude,
                    grid_point.normalized_longitude,
                    *(format_utc_datetime(value) for value in normalized),
                ),
            ).fetchall()
        return MappingProxyType(
            {
                record.time: record
                for record in (_archive_record(row) for row in rows)
            }
        )

    def insert_historical_hours(
        self, hours: Sequence[HistoricalMesanHour]
    ) -> tuple[int, int]:
        """Insert historical gaps while retaining equal/higher-priority rows."""

        if not hours:
            return 0, 0
        grid_points = {
            (
                hour.grid_point.normalized_latitude,
                hour.grid_point.normalized_longitude,
            )
            for hour in hours
        }
        if len(grid_points) != 1:
            raise MesanHistoryError("One historical import cannot mix MESAN grids")
        self.initialize()
        inserted_at = format_utc_datetime(datetime.now(UTC))
        grid_latitude, grid_longitude = next(iter(grid_points))
        normalized_times = [format_utc_datetime(hour.time) for hour in hours]
        with sqlite3.connect(self.path) as connection:
            placeholders = ",".join("?" for _ in normalized_times)
            existing = {
                str(row[0])
                for row in connection.execute(
                    f"""
                    SELECT timestamp_utc FROM mesan_hourly_analysis
                    WHERE grid_latitude=? AND grid_longitude=?
                      AND timestamp_utc IN ({placeholders})
                    """,
                    (grid_latitude, grid_longitude, *normalized_times),
                )
            }
            rows = [
                (
                    hour.grid_point.normalized_latitude,
                    hour.grid_point.normalized_longitude,
                    format_utc_datetime(hour.time),
                    hour.air_temperature_c,
                    hour.precipitation_1h_mm,
                    hour.relative_humidity_percent,
                    hour.wind_speed_m_s,
                    hour.source_api,
                    hour.source_product,
                    hour.source_version,
                    hour.source_url,
                    hour.source_file,
                    hour.grid_identity,
                    hour.sampling_method,
                    hour.requested_point.latitude,
                    hour.requested_point.longitude,
                    historical_units_json(hour),
                    HISTORICAL_SOURCE_PRIORITY,
                    format_utc_datetime(hour.reference_time),
                    format_utc_datetime(hour.created_time),
                    inserted_at,
                )
                for hour in hours
            ]
            connection.executemany(
                """
                INSERT INTO mesan_hourly_analysis (
                    grid_latitude, grid_longitude, timestamp_utc,
                    air_temperature_c, precipitation_1h_mm,
                    relative_humidity_percent, wind_speed_m_s,
                    source_api, source_product, source_version, source_url,
                    source_file, grid_identity, sampling_method,
                    requested_latitude, requested_longitude, source_units_json,
                    source_priority, reference_time_utc, created_time_utc,
                    inserted_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(grid_latitude, grid_longitude, timestamp_utc)
                DO UPDATE SET
                    air_temperature_c=excluded.air_temperature_c,
                    precipitation_1h_mm=excluded.precipitation_1h_mm,
                    relative_humidity_percent=excluded.relative_humidity_percent,
                    wind_speed_m_s=excluded.wind_speed_m_s,
                    source_api=excluded.source_api,
                    source_product=excluded.source_product,
                    source_version=excluded.source_version,
                    source_url=excluded.source_url,
                    source_file=excluded.source_file,
                    grid_identity=excluded.grid_identity,
                    sampling_method=excluded.sampling_method,
                    requested_latitude=excluded.requested_latitude,
                    requested_longitude=excluded.requested_longitude,
                    source_units_json=excluded.source_units_json,
                    source_priority=excluded.source_priority,
                    reference_time_utc=excluded.reference_time_utc,
                    created_time_utc=excluded.created_time_utc,
                    inserted_at_utc=excluded.inserted_at_utc
                WHERE excluded.source_priority > mesan_hourly_analysis.source_priority
                """,
                rows,
            )
        inserted = sum(timestamp not in existing for timestamp in normalized_times)
        return inserted, len(hours) - inserted

    def get_time_bounds(
        self, grid_point: MesanGridPoint
    ) -> tuple[datetime | None, datetime | None]:
        with self._existing_connection() as connection:
            row = connection.execute(
                """
                SELECT MIN(timestamp_utc), MAX(timestamp_utc)
                FROM mesan_hourly_analysis
                WHERE grid_latitude=? AND grid_longitude=?
                """,
                (
                    grid_point.normalized_latitude,
                    grid_point.normalized_longitude,
                ),
            ).fetchone()
        if row is None or row[0] is None:
            return None, None
        return (
            parse_utc_datetime(row[0], "earliest archive timestamp"),
            parse_utc_datetime(row[1], "latest archive timestamp"),
        )

    def latest_time_at_or_before(
        self, grid_point: MesanGridPoint, cutoff: datetime
    ) -> datetime | None:
        """Return the newest stored UTC hour not later than ``cutoff``."""

        if cutoff.tzinfo is None:
            raise ValueError("Weather-history cutoff must have a timezone")
        normalized_cutoff = cutoff.astimezone(UTC)
        with self._existing_connection() as connection:
            row = connection.execute(
                """
                SELECT MAX(timestamp_utc)
                FROM mesan_hourly_analysis
                WHERE grid_latitude=? AND grid_longitude=? AND timestamp_utc<=?
                """,
                (
                    grid_point.normalized_latitude,
                    grid_point.normalized_longitude,
                    format_utc_datetime(normalized_cutoff),
                ),
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return parse_utc_datetime(row[0], "latest archive timestamp before cutoff")

    def list_grid_points(self) -> tuple[MesanGridPoint, ...]:
        with self._existing_connection() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT grid_latitude, grid_longitude
                FROM mesan_hourly_analysis
                ORDER BY grid_latitude, grid_longitude
                """
            ).fetchall()
        return tuple(MesanGridPoint(float(row[0]), float(row[1])) for row in rows)

    def find_nearest_grid_point(
        self, latitude: float, longitude: float
    ) -> MesanGridPoint:
        requested = MesanGridPoint(latitude, longitude)
        points = self.list_grid_points()
        if not points:
            raise MesanHistoryError("MESAN archive contains no grid points")
        return min(
            points,
            key=lambda point: _haversine_distance_m(requested, point),
        )

    def read_window(
        self,
        grid_point: MesanGridPoint,
        *,
        start_exclusive: datetime,
        end_inclusive: datetime,
    ) -> tuple[MesanArchiveRecord, ...]:
        start = _normalize_time(start_exclusive)
        end = _normalize_time(end_inclusive)
        with self._existing_connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM mesan_hourly_analysis
                WHERE grid_latitude=? AND grid_longitude=?
                  AND timestamp_utc>? AND timestamp_utc<=?
                ORDER BY timestamp_utc
                """,
                (
                    grid_point.normalized_latitude,
                    grid_point.normalized_longitude,
                    format_utc_datetime(start),
                    format_utc_datetime(end),
                ),
            ).fetchall()
        return tuple(_archive_record(row) for row in rows)

    def read_to_time(
        self, grid_point: MesanGridPoint, target_time: datetime
    ) -> tuple[MesanArchiveRecord, ...]:
        target = _normalize_time(target_time)
        with self._existing_connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM mesan_hourly_analysis
                WHERE grid_latitude=? AND grid_longitude=? AND timestamp_utc<=?
                ORDER BY timestamp_utc DESC
                """,
                (
                    grid_point.normalized_latitude,
                    grid_point.normalized_longitude,
                    format_utc_datetime(target),
                ),
            ).fetchall()
        return tuple(_archive_record(row) for row in rows)


def _archive_record(row: sqlite3.Row) -> MesanArchiveRecord:
    try:
        source_units = json.loads(str(row["source_units_json"]))
    except json.JSONDecodeError as exc:
        raise MesanHistoryError("Invalid source_units_json in MESAN archive") from exc
    if not isinstance(source_units, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in source_units.items()
    ):
        raise MesanHistoryError("Invalid source-unit mapping in MESAN archive")
    return MesanArchiveRecord(
        grid_point=MesanGridPoint(float(row["grid_latitude"]), float(row["grid_longitude"])),
        time=parse_utc_datetime(row["timestamp_utc"], "archive timestamp"),
        air_temperature_c=row["air_temperature_c"],
        precipitation_1h_mm=row["precipitation_1h_mm"],
        relative_humidity_percent=row["relative_humidity_percent"],
        wind_speed_m_s=row["wind_speed_m_s"],
        source_api=str(row["source_api"]),
        source_product=str(row["source_product"]),
        source_version=str(row["source_version"]),
        source_url=str(row["source_url"]),
        source_file=str(row["source_file"]),
        grid_identity=str(row["grid_identity"]),
        sampling_method=str(row["sampling_method"]),
        requested_latitude=(
            float(row["requested_latitude"])
            if row["requested_latitude"] is not None
            else None
        ),
        requested_longitude=(
            float(row["requested_longitude"])
            if row["requested_longitude"] is not None
            else None
        ),
        source_units=MappingProxyType(source_units),
        source_priority=int(row["source_priority"]),
        reference_time=parse_utc_datetime(row["reference_time_utc"], "reference time"),
        created_time=parse_utc_datetime(row["created_time_utc"], "created time"),
    )


def ingest_recent_mesan(
    latitude: float,
    longitude: float,
    *,
    archive_path: str | Path = DEFAULT_MESAN_HISTORY_DATABASE,
    client: MesanPointClientProtocol | None = None,
) -> MesanIngestionResult:
    """Fetch first, then append valid new hours without rewriting the archive."""

    resolved_client = SmhiMesanPointClient() if client is None else client
    # Fetch and parse before touching SQLite. A failed response therefore cannot
    # create or mutate the archive.
    analysis = resolved_client.get_point_analysis(latitude, longitude)
    archive = MesanHistoryArchive(archive_path)
    statistics = archive.insert_point_analysis(analysis)
    earliest, latest = archive.get_time_bounds(analysis.grid_point)
    return MesanIngestionResult(
        requested_point=analysis.requested_point,
        grid_point=analysis.grid_point,
        statistics=statistics,
        database_path=str(archive.path),
        earliest_stored_time=earliest,
        latest_stored_time=latest,
    )


def _latest_time_for_location(
    archive: MesanHistoryArchive,
    latitude: float,
    longitude: float,
) -> datetime | None:
    if not archive.path.is_file():
        return None
    try:
        grid_point = archive.find_nearest_grid_point(latitude, longitude)
    except MesanHistoryError:
        return None
    _, latest = archive.get_time_bounds(grid_point)
    return latest


def _overlap_difference(
    historical: HistoricalMesanHour,
    retained: MesanArchiveRecord,
) -> MesanOverlapDifference:
    existing_values = (
        retained.air_temperature_c,
        retained.precipitation_1h_mm,
        retained.relative_humidity_percent,
        retained.wind_speed_m_s,
    )
    if any(value is None for value in existing_values):
        raise MesanHistoryError(
            f"Stored overlap at {format_utc_datetime(retained.time)} is incomplete"
        )
    return MesanOverlapDifference(
        time=historical.time,
        retained_source_product=retained.source_product,
        historical_source_product=historical.source_product,
        air_temperature_difference_c=(
            historical.air_temperature_c - float(retained.air_temperature_c)
        ),
        precipitation_1h_difference_mm=(
            historical.precipitation_1h_mm - float(retained.precipitation_1h_mm)
        ),
        relative_humidity_difference_percent=(
            historical.relative_humidity_percent
            - float(retained.relative_humidity_percent)
        ),
        wind_speed_difference_m_s=(
            historical.wind_speed_m_s - float(retained.wind_speed_m_s)
        ),
    )


def backfill_mesan_history(
    latitude: float,
    longitude: float,
    *,
    days: int,
    archive_path: str | Path = DEFAULT_MESAN_HISTORY_DATABASE,
    cache_path: str | Path = DEFAULT_MESAN_ARCHIVE_CACHE,
    end_time: datetime | None = None,
    feed_client: MesanArchiveFeedProtocol | None = None,
    point_reader: MesanHistoricalPointReaderProtocol | None = None,
    max_workers: int = 8,
) -> MesanHistoricalBackfillResult:
    """Backfill a complete inclusive hourly window without replacing live rows."""

    if days <= 0:
        raise ValueError("days must be positive")
    requested_point = MesanGridPoint(latitude, longitude)
    archive = MesanHistoryArchive(archive_path)
    resolved_end = end_time
    if resolved_end is None:
        resolved_end = _latest_time_for_location(archive, latitude, longitude)
    if resolved_end is None:
        raise MesanHistoryError(
            "No target hour is available; ingest live MESAN first or supply end_time"
        )
    target = _normalize_time(resolved_end)
    requested_hours = days * 24
    start = target - timedelta(hours=requested_hours - 1)

    resolved_feed = MesanArchiveFeedClient() if feed_client is None else feed_client
    assets, _ = resolved_feed.list_period(start, target)
    discovered_times = {asset.timestamp for asset in assets if hasattr(asset, "timestamp")}
    if len(discovered_times) != len(assets):
        raise MesanHistoryError("Historical MESAN discovery returned duplicate timestamps")
    if not assets:
        raise MesanHistoryError("No historical MESAN assets were found for the requested period")

    resolved_reader = (
        MesanHistoricalGribPointReader(cache_directory=cache_path)
        if point_reader is None
        else point_reader
    )
    layout, selection, hours = resolved_reader.sample_assets(
        assets,
        latitude=latitude,
        longitude=longitude,
        max_workers=max_workers,
    )
    if len(hours) != len(assets) or len({hour.time for hour in hours}) != len(hours):
        raise MesanHistoryError("Historical MESAN parser did not return one unique hour per asset")
    if any(not start <= hour.time <= target for hour in hours):
        raise MesanHistoryError("Historical MESAN parser returned an hour outside the request")

    # All discovery, downloading and parsing is complete before SQLite is touched.
    archive.initialize()
    existing = archive.existing_records(
        selection.grid_point,
        [hour.time for hour in hours],
    )
    overlaps = tuple(
        _overlap_difference(hour, existing[hour.time])
        for hour in hours
        if hour.time in existing
        and existing[hour.time].source_priority > HISTORICAL_SOURCE_PRIORITY
    )
    inserted, already_present = archive.insert_historical_hours(hours)
    earliest, latest = archive.get_time_bounds(selection.grid_point)
    grid_identity = getattr(layout, "grid_identity", selection.grid_identity)
    return MesanHistoricalBackfillResult(
        requested_point=requested_point,
        grid_point=selection.grid_point,
        requested_start_time=start,
        requested_end_time=target,
        statistics=HistoricalBackfillStatistics(
            discovered=len(assets),
            parsed=len(hours),
            inserted=inserted,
            already_present=already_present,
            invalid=0,
            unavailable=requested_hours - len(discovered_times),
        ),
        overlap_differences=overlaps,
        database_path=str(archive.path),
        cache_path=str(Path(cache_path).resolve()),
        grid_identity=str(grid_identity),
        grid_crs_wkt=selection.crs_wkt,
        grid_resolution_m=selection.grid_resolution_m,
        sampling_method=selection.sampling_method,
        earliest_stored_time=earliest,
        latest_stored_time=latest,
        precedence_policy=(
            "live MESAN2G v3 priority 100 > historical MESAN GRIDPP priority 50; "
            "historical rows never overwrite live rows"
        ),
    )


def _coverage_metric(
    records: Sequence[MesanArchiveRecord],
    *,
    expected_hours: int,
    attribute: str,
    unit: str,
    reducer: Callable[[Sequence[float]], float],
) -> WeatherAggregate:
    values = [
        float(value)
        for record in records
        if (value := getattr(record, attribute)) is not None
    ]
    available = len(values)
    fraction = available / expected_hours
    if available == expected_hours:
        status = CoverageStatus.FULL
        value = reducer(values)
    elif available:
        status = CoverageStatus.PARTIAL
        value = None
    else:
        status = CoverageStatus.INSUFFICIENT
        value = None
    return WeatherAggregate(
        value=value,
        unit=unit,
        expected_hours=expected_hours,
        available_hours=available,
        coverage_fraction=fraction,
        status=status,
    )


def _sum(values: Sequence[float]) -> float:
    return float(sum(values))


def _mean(values: Sequence[float]) -> float:
    return float(sum(values) / len(values))


def _threshold_required_metric() -> WeatherAggregate:
    return WeatherAggregate(
        value=None,
        unit="days",
        expected_hours=0,
        available_hours=0,
        coverage_fraction=0.0,
        status=CoverageStatus.THRESHOLD_REQUIRED,
    )


def _dry_period_metric(
    archive: MesanHistoryArchive,
    grid_point: MesanGridPoint,
    target_time: datetime,
    threshold_mm: float | None,
) -> WeatherAggregate:
    if threshold_mm is None:
        return _threshold_required_metric()
    if not math.isfinite(threshold_mm) or threshold_mm <= 0:
        raise ValueError("significant_rain_threshold_mm must be positive and finite")
    records = archive.read_to_time(grid_point, target_time)
    expected_time = target_time
    available = 0
    for dry_hours, record in enumerate(records):
        if record.time != expected_time or record.precipitation_1h_mm is None:
            expected = available + 1
            return WeatherAggregate(
                value=None,
                unit="days",
                expected_hours=expected,
                available_hours=available,
                coverage_fraction=available / expected,
                status=(
                    CoverageStatus.PARTIAL
                    if available
                    else CoverageStatus.INSUFFICIENT
                ),
            )
        available += 1
        if record.precipitation_1h_mm >= threshold_mm:
            return WeatherAggregate(
                value=dry_hours / 24,
                unit="days",
                expected_hours=available,
                available_hours=available,
                coverage_fraction=1.0,
                status=CoverageStatus.FULL,
            )
        expected_time -= timedelta(hours=1)
    return WeatherAggregate(
        value=None,
        unit="days",
        expected_hours=available + 1,
        available_hours=available,
        coverage_fraction=(available / (available + 1) if available else 0.0),
        status=CoverageStatus.UNBOUNDED,
    )


def get_weather_history_features_from_archive(
    archive: MesanHistoryArchive,
    grid_point: MesanGridPoint,
    target_time: datetime,
    *,
    significant_rain_threshold_mm: float | None = None,
) -> WeatherHistoryFeatures:
    """Aggregate one MESAN point using one shared 30-day archive read."""

    target = _normalize_time(target_time)
    records = archive.read_window(
        grid_point,
        start_exclusive=target - timedelta(days=30),
        end_inclusive=target,
    )

    def window(days: int) -> tuple[MesanArchiveRecord, ...]:
        cutoff = target - timedelta(days=days)
        return tuple(
            record for record in records if cutoff < record.time <= target
        )

    rain = {
        days: _coverage_metric(
            window(days),
            expected_hours=days * 24,
            attribute="precipitation_1h_mm",
            unit="mm",
            reducer=_sum,
        )
        for days in (1, 3, 7, 14, 21, 30)
    }
    temperature = {
        days: _coverage_metric(
            window(days),
            expected_hours=days * 24,
            attribute="air_temperature_c",
            unit="degrees_celsius",
            reducer=_mean,
        )
        for days in (3, 7, 14)
    }
    humidity = {
        days: _coverage_metric(
            window(days),
            expected_hours=days * 24,
            attribute="relative_humidity_percent",
            unit="percent",
            reducer=_mean,
        )
        for days in (3, 7)
    }
    dry_metric = _dry_period_metric(
        archive,
        grid_point,
        target,
        significant_rain_threshold_mm,
    )
    return WeatherHistoryFeatures(
        grid_point=grid_point,
        target_time=target,
        rain_1d_mm=rain[1],
        rain_3d_mm=rain[3],
        rain_7d_mm=rain[7],
        rain_14d_mm=rain[14],
        rain_21d_mm=rain[21],
        rain_30d_mm=rain[30],
        temp_mean_3d_c=temperature[3],
        temp_mean_7d_c=temperature[7],
        temp_mean_14d_c=temperature[14],
        relative_humidity_mean_3d=humidity[3],
        relative_humidity_mean_7d=humidity[7],
        days_since_significant_rain=dry_metric,
        dry_spell_length=dry_metric,
        significant_rain_threshold_mm=significant_rain_threshold_mm,
    )


def get_weather_history_features(
    grid_point: MesanGridPoint,
    target_time: datetime,
    *,
    archive_path: str | Path = DEFAULT_MESAN_HISTORY_DATABASE,
    significant_rain_threshold_mm: float | None = None,
) -> WeatherHistoryFeatures:
    """Return values only for complete windows; partial windows retain coverage."""

    archive = MesanHistoryArchive(archive_path)
    try:
        return get_weather_history_features_from_archive(
            archive,
            grid_point,
            target_time,
            significant_rain_threshold_mm=significant_rain_threshold_mm,
        )
    finally:
        archive.close()


def _haversine_distance_m(first: MesanGridPoint, second: MesanGridPoint) -> float:
    radius_m = 6_371_008.8
    first_latitude = math.radians(first.latitude)
    second_latitude = math.radians(second.latitude)
    latitude_delta = second_latitude - first_latitude
    longitude_delta = math.radians(second.longitude - first.longitude)
    value = (
        math.sin(latitude_delta / 2) ** 2
        + math.cos(first_latitude)
        * math.cos(second_latitude)
        * math.sin(longitude_delta / 2) ** 2
    )
    return 2 * radius_m * math.asin(min(1.0, math.sqrt(value)))


__all__ = [
    "DEFAULT_MESAN_ARCHIVE_CACHE",
    "DEFAULT_MESAN_HISTORY_DATABASE",
    "HISTORICAL_SOURCE_PRODUCT",
    "MESAN_SOURCE_NAME",
    "MESAN_SOURCE_VERSION",
    "CoverageStatus",
    "HistoricalBackfillStatistics",
    "IngestionStatistics",
    "MesanArchiveRecord",
    "MesanHistoricalBackfillResult",
    "MesanHistoryArchive",
    "MesanHistoryError",
    "MesanIngestionResult",
    "MesanOverlapDifference",
    "WeatherAggregate",
    "WeatherHistoryFeatures",
    "backfill_mesan_history",
    "get_weather_history_features",
    "ingest_recent_mesan",
]
