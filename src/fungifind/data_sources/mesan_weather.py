"""Coverage-aware WeatherDataSource backed by the local MESAN SQLite archive."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from threading import RLock

from fungifind.data_sources.smhi_mesan import MesanGridPoint, format_utc_datetime
from fungifind.data_sources.weather_history import (
    DEFAULT_MESAN_HISTORY_DATABASE,
    CoverageStatus,
    MesanArchiveRecord,
    MesanHistoryArchive,
    MesanHistoryError,
    WeatherAggregate,
    WeatherHistoryFeatures,
    get_weather_history_features_from_archive,
)
from fungifind.models import (
    DataSourceMetadata,
    DynamicWeatherFeatures,
    FeatureProvenance,
    FeatureSnapshot,
    Location,
)

MESAN_WEATHER_SOURCE_NAME = "smhi_mesan_weather_history_v1"
DEFAULT_MAX_GRID_DISTANCE_M = 5_000.0

_AGGREGATE_FEATURES: Mapping[str, tuple[str, int, str]] = {
    "rainfall_1d_mm": ("rain_1d_mm", 1, "sum_hourly_precipitation"),
    "rainfall_3d_mm": ("rain_3d_mm", 3, "sum_hourly_precipitation"),
    "rainfall_7d_mm": ("rain_7d_mm", 7, "sum_hourly_precipitation"),
    "rainfall_14d_mm": ("rain_14d_mm", 14, "sum_hourly_precipitation"),
    "rainfall_21d_mm": ("rain_21d_mm", 21, "sum_hourly_precipitation"),
    "rainfall_30d_mm": ("rain_30d_mm", 30, "sum_hourly_precipitation"),
    "temp_mean_3d_c": ("temp_mean_3d_c", 3, "mean_hourly_air_temperature"),
    "temp_mean_7d_c": ("temp_mean_7d_c", 7, "mean_hourly_air_temperature"),
    "temp_mean_14d_c": ("temp_mean_14d_c", 14, "mean_hourly_air_temperature"),
    "relative_humidity_mean_3d_percent": (
        "relative_humidity_mean_3d",
        3,
        "mean_hourly_relative_humidity",
    ),
    "relative_humidity_mean_7d_percent": (
        "relative_humidity_mean_7d",
        7,
        "mean_hourly_relative_humidity",
    ),
}


def _distance_m(first: MesanGridPoint, second: MesanGridPoint) -> float:
    radius_m = 6_371_008.8
    first_latitude = math.radians(first.latitude)
    second_latitude = math.radians(second.latitude)
    latitude_delta = second_latitude - first_latitude
    longitude_delta = math.radians(second.longitude - first.longitude)
    haversine = (
        math.sin(latitude_delta / 2) ** 2
        + math.cos(first_latitude)
        * math.cos(second_latitude)
        * math.sin(longitude_delta / 2) ** 2
    )
    return 2 * radius_m * math.asin(math.sqrt(haversine))


def _cutoff(value: date | datetime) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("MESAN target datetime must have a timezone")
        return value.astimezone(UTC)
    return datetime.combine(value, time.max, tzinfo=UTC)


def _full_value(aggregate: WeatherAggregate) -> float | None:
    if aggregate.status is CoverageStatus.FULL:
        return aggregate.value
    return None


def _joined(values: Sequence[str]) -> str:
    return ",".join(sorted(set(values)))


@dataclass(frozen=True, slots=True)
class MesanWeatherResolution:
    requested_point: MesanGridPoint
    grid_point: MesanGridPoint
    grid_distance_m: float
    target_time: datetime


class MesanWeatherHistoryDataSource:
    """Resolve a nearby stored MESAN grid point and expose full aggregates only."""

    def __init__(
        self,
        archive_path: str | Path = DEFAULT_MESAN_HISTORY_DATABASE,
        *,
        max_grid_distance_m: float = DEFAULT_MAX_GRID_DISTANCE_M,
        quality: float = 0.95,
        significant_rain_threshold_mm: float | None = None,
    ) -> None:
        if not math.isfinite(max_grid_distance_m) or max_grid_distance_m <= 0:
            raise ValueError("max_grid_distance_m must be positive and finite")
        if not 0 <= quality <= 1:
            raise ValueError("quality must be between 0 and 1")
        self.archive = MesanHistoryArchive(archive_path)
        self.max_grid_distance_m = max_grid_distance_m
        self.quality = quality
        self.significant_rain_threshold_mm = significant_rain_threshold_mm
        self._grid_points_lock = RLock()
        self._grid_points: tuple[MesanGridPoint, ...] | None = None

    def available_grid_points(self) -> tuple[MesanGridPoint, ...]:
        """Return the stable local archive grid set, loading it only once."""

        with self._grid_points_lock:
            if self._grid_points is None:
                self._grid_points = self.archive.list_grid_points()
            if not self._grid_points:
                raise MesanHistoryError("MESAN archive contains no grid points")
            return self._grid_points

    def snap_grid_point(self, location: Location) -> tuple[MesanGridPoint, float]:
        """Resolve only the nearest stored point for viewport grouping."""

        requested = MesanGridPoint(location.latitude, location.longitude)
        grid_point = min(
            self.available_grid_points(),
            key=lambda point: _distance_m(requested, point),
        )
        distance = _distance_m(requested, grid_point)
        if distance > self.max_grid_distance_m:
            raise MesanHistoryError(
                "Nearest stored MESAN grid point is "
                f"{distance:.0f} m away, beyond the configured "
                f"{self.max_grid_distance_m:.0f} m limit; backfill this location first"
            )
        return grid_point, distance

    def resolve_snapped(
        self,
        location: Location,
        target_date: date | datetime,
        grid_point: MesanGridPoint,
        grid_distance_m: float,
    ) -> MesanWeatherResolution:
        """Resolve the target hour for an already-snapped viewport group."""

        target_time = self.archive.latest_time_at_or_before(
            grid_point, _cutoff(target_date)
        )
        if target_time is None:
            raise MesanHistoryError(
                "Selected MESAN grid point has no stored hour on or before the target"
            )
        return MesanWeatherResolution(
            MesanGridPoint(location.latitude, location.longitude),
            grid_point,
            grid_distance_m,
            target_time,
        )

    def resolve(
        self, location: Location, target_date: date | datetime
    ) -> MesanWeatherResolution:
        grid_point, distance = self.snap_grid_point(location)
        return self.resolve_snapped(
            location,
            target_date,
            grid_point,
            distance,
        )

    def get_history_features_for_resolution(
        self, resolution: MesanWeatherResolution
    ) -> WeatherHistoryFeatures:
        return get_weather_history_features_from_archive(
            self.archive,
            resolution.grid_point,
            resolution.target_time,
            significant_rain_threshold_mm=self.significant_rain_threshold_mm,
        )

    def get_history_features(
        self, location: Location, target_date: date | datetime
    ) -> tuple[WeatherHistoryFeatures, MesanWeatherResolution]:
        resolution = self.resolve(location, target_date)
        history = self.get_history_features_for_resolution(resolution)
        return history, resolution

    def _provenance(
        self,
        *,
        feature_name: str,
        aggregate: WeatherAggregate,
        days: int,
        method: str,
        records: Sequence[MesanArchiveRecord],
        resolution: MesanWeatherResolution,
    ) -> FeatureProvenance:
        relevant = tuple(
            record
            for record in records
            if record.time > resolution.target_time - timedelta(days=days)
        )
        grid_identities = _joined([record.grid_identity for record in relevant])
        source_products = _joined([record.source_product for record in relevant])
        source_versions = _joined([record.source_version for record in relevant])
        source_apis = _joined([record.source_api for record in relevant])
        sampling_methods = _joined([record.sampling_method for record in relevant])
        source_files = {record.source_file for record in relevant if record.source_file}
        semantic_status = (
            "validated_full_coverage"
            if aggregate.status is CoverageStatus.FULL
            else f"unavailable_{aggregate.status.value}_coverage"
        )
        return FeatureProvenance(
            source_name=MESAN_WEATHER_SOURCE_NAME,
            quality=self.quality if aggregate.status is CoverageStatus.FULL else 0.0,
            is_mock=False,
            semantic_status=semantic_status,
            source_path=str(self.archive.path),
            raw_value=aggregate.value,
            interpreted_value=_full_value(aggregate),
            grid_signature=grid_identities or None,
            details={
                "feature": feature_name,
                "coverage_status": aggregate.status.value,
                "coverage_fraction": aggregate.coverage_fraction,
                "expected_hours": aggregate.expected_hours,
                "available_hours": aggregate.available_hours,
                "unit": aggregate.unit,
                "aggregate_method": method,
                "window_days": days,
                "target_time_utc": format_utc_datetime(resolution.target_time),
                "requested_latitude": resolution.requested_point.latitude,
                "requested_longitude": resolution.requested_point.longitude,
                "grid_latitude": resolution.grid_point.latitude,
                "grid_longitude": resolution.grid_point.longitude,
                "grid_distance_m": round(resolution.grid_distance_m, 3),
                "source_products": source_products,
                "source_versions": source_versions,
                "source_apis": source_apis,
                "sampling_methods": sampling_methods,
                "source_file_count": len(source_files),
                "hourly_provenance": "mesan_hourly_analysis rows in source_path",
            },
        )

    def get_features(
        self, location: Location, target_date: date | datetime
    ) -> FeatureSnapshot[DynamicWeatherFeatures]:
        resolution = self.resolve(location, target_date)
        return self.get_features_for_resolution(resolution)

    def get_features_for_resolution(
        self, resolution: MesanWeatherResolution
    ) -> FeatureSnapshot[DynamicWeatherFeatures]:
        """Aggregate one already-resolved MESAN group without re-snapping it."""

        history = self.get_history_features_for_resolution(resolution)
        records = self.archive.read_window(
            resolution.grid_point,
            start_exclusive=resolution.target_time - timedelta(days=30),
            end_inclusive=resolution.target_time,
        )
        feature_values: dict[str, float | None] = {}
        provenance: dict[str, FeatureProvenance] = {}
        completeness: dict[str, str] = {}
        for feature_name, (aggregate_name, days, method) in _AGGREGATE_FEATURES.items():
            aggregate = getattr(history, aggregate_name)
            feature_values[feature_name] = _full_value(aggregate)
            completeness[feature_name] = aggregate.status.value
            provenance[feature_name] = self._provenance(
                feature_name=feature_name,
                aggregate=aggregate,
                days=days,
                method=method,
                records=records,
                resolution=resolution,
            )

        for feature_name, aggregate_name in (
            ("days_since_significant_rain", "days_since_significant_rain"),
            ("dry_spell_length_days", "dry_spell_length"),
        ):
            aggregate = getattr(history, aggregate_name)
            feature_values[feature_name] = _full_value(aggregate)
            completeness[feature_name] = aggregate.status.value
            provenance[feature_name] = self._provenance(
                feature_name=feature_name,
                aggregate=aggregate,
                days=30,
                method="disabled_without_configured_significant_rain_threshold",
                records=records,
                resolution=resolution,
            )

        full_count = sum(status == CoverageStatus.FULL.value for status in completeness.values())
        return FeatureSnapshot(
            features=DynamicWeatherFeatures(**feature_values),
            metadata=DataSourceMetadata(
                source_name=MESAN_WEATHER_SOURCE_NAME,
                quality=self.quality,
                is_mock=False,
                details={
                    "database": str(self.archive.path),
                    "target_time_utc": format_utc_datetime(resolution.target_time),
                    "grid_latitude": resolution.grid_point.latitude,
                    "grid_longitude": resolution.grid_point.longitude,
                    "grid_distance_m": round(resolution.grid_distance_m, 3),
                    "full_feature_count": full_count,
                    "feature_count": len(completeness),
                    "dry_period_scoring": "disabled",
                },
            ),
            feature_provenance=provenance,
        )


def weather_completeness(
    snapshot: FeatureSnapshot[DynamicWeatherFeatures],
) -> Mapping[str, str]:
    """Return explicit per-feature coverage labels retained in provenance."""

    return {
        name: str(item.details["coverage_status"])
        for name, item in snapshot.feature_provenance.items()
        if "coverage_status" in item.details
    }
