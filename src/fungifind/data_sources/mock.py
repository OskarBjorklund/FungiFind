"""Synthetic samples that make the full pipeline runnable without external services."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from math import asin, cos, radians, sin, sqrt

from fungifind.models import (
    DataSourceMetadata,
    DynamicWeatherFeatures,
    FeatureSnapshot,
    Location,
    SoilType,
    StaticHabitatFeatures,
)


@dataclass(frozen=True, slots=True)
class _HabitatSample:
    sample_id: str
    location: Location
    features: StaticHabitatFeatures


@dataclass(frozen=True, slots=True)
class _WeatherSample:
    sample_id: str
    location: Location
    baseline_date: date
    features: DynamicWeatherFeatures


def _distance_km(first: Location, second: Location) -> float:
    """Haversine distance, sufficient only for choosing a nearby mock sample."""

    earth_radius_km = 6371.0
    lat_1, lat_2 = radians(first.latitude), radians(second.latitude)
    delta_lat = lat_2 - lat_1
    delta_lon = radians(second.longitude - first.longitude)
    a = sin(delta_lat / 2) ** 2 + cos(lat_1) * cos(lat_2) * sin(delta_lon / 2) ** 2
    return 2 * earth_radius_km * asin(sqrt(a))


_HABITAT_SAMPLES = (
    _HabitatSample(
        sample_id="stockholm_mixed_forest",
        location=Location(59.412, 18.132),
        features=StaticHabitatFeatures(
            forest_cover_fraction=0.88,
            canopy_density_fraction=0.72,
            spruce_fraction=0.42,
            pine_fraction=0.22,
            birch_fraction=0.20,
            other_deciduous_fraction=0.08,
            beech_fraction=0.00,
            oak_fraction=0.08,
            other_tree_fraction=0.00,
            soil_moisture_index=0.68,
            topographic_moisture_index=0.62,
            elevation_m=36.0,
            slope_degrees=7.0,
            aspect_degrees=155.0,
            distance_to_water_m=420.0,
            tree_height_m=17.0,
            soil_type=SoilType.TILL,
        ),
    ),
    _HabitatSample(
        sample_id="smaland_conifer_forest",
        location=Location(57.250, 14.600),
        features=StaticHabitatFeatures(
            forest_cover_fraction=0.94,
            canopy_density_fraction=0.79,
            spruce_fraction=0.62,
            pine_fraction=0.18,
            birch_fraction=0.12,
            other_deciduous_fraction=0.05,
            beech_fraction=0.02,
            oak_fraction=0.01,
            other_tree_fraction=0.00,
            soil_moisture_index=0.74,
            topographic_moisture_index=0.76,
            elevation_m=185.0,
            slope_degrees=5.0,
            aspect_degrees=40.0,
            distance_to_water_m=790.0,
            tree_height_m=20.0,
            soil_type=SoilType.TILL,
        ),
    ),
    _HabitatSample(
        sample_id="jamtland_open_pine",
        location=Location(63.100, 14.300),
        features=StaticHabitatFeatures(
            forest_cover_fraction=0.69,
            canopy_density_fraction=0.48,
            spruce_fraction=0.20,
            pine_fraction=0.55,
            birch_fraction=0.20,
            other_deciduous_fraction=0.05,
            other_tree_fraction=0.00,
            soil_moisture_index=0.50,
            topographic_moisture_index=0.46,
            elevation_m=425.0,
            slope_degrees=12.0,
            aspect_degrees=220.0,
            distance_to_water_m=1600.0,
            tree_height_m=11.0,
            soil_type=SoilType.SANDY,
        ),
    ),
)


_WEATHER_SAMPLES = (
    _WeatherSample(
        sample_id="stockholm_august_weather",
        location=Location(59.412, 18.132),
        baseline_date=date(2026, 8, 23),
        features=DynamicWeatherFeatures(
            rainfall_1d_mm=2.0,
            rainfall_3d_mm=8.0,
            rainfall_7d_mm=24.0,
            rainfall_14d_mm=48.0,
            rainfall_21d_mm=61.0,
            rainfall_30d_mm=82.0,
            temp_mean_3d_c=15.6,
            temp_mean_7d_c=15.2,
            temp_mean_14d_c=14.7,
            relative_humidity_mean_3d_percent=78.0,
            relative_humidity_mean_7d_percent=75.0,
            mean_temperature_c=15.2,
            min_temperature_c=9.6,
            max_temperature_c=20.8,
            estimated_current_soil_moisture_index=0.70,
            evapotranspiration_7d_mm=16.0,
            dry_days_count_14d=6,
            temperature_trend_c_per_day=-0.25,
        ),
    ),
    _WeatherSample(
        sample_id="smaland_august_weather",
        location=Location(57.250, 14.600),
        baseline_date=date(2026, 8, 23),
        features=DynamicWeatherFeatures(
            rainfall_1d_mm=5.0,
            rainfall_3d_mm=15.0,
            rainfall_7d_mm=38.0,
            rainfall_14d_mm=64.0,
            rainfall_21d_mm=91.0,
            rainfall_30d_mm=118.0,
            temp_mean_3d_c=14.8,
            temp_mean_7d_c=14.4,
            temp_mean_14d_c=13.9,
            relative_humidity_mean_3d_percent=82.0,
            relative_humidity_mean_7d_percent=79.0,
            mean_temperature_c=14.4,
            min_temperature_c=8.5,
            max_temperature_c=19.8,
            estimated_current_soil_moisture_index=0.79,
            evapotranspiration_7d_mm=13.0,
            dry_days_count_14d=4,
            temperature_trend_c_per_day=-0.15,
        ),
    ),
    _WeatherSample(
        sample_id="jamtland_august_weather",
        location=Location(63.100, 14.300),
        baseline_date=date(2026, 8, 23),
        features=DynamicWeatherFeatures(
            rainfall_1d_mm=0.0,
            rainfall_3d_mm=3.0,
            rainfall_7d_mm=11.0,
            rainfall_14d_mm=29.0,
            rainfall_21d_mm=47.0,
            rainfall_30d_mm=69.0,
            temp_mean_3d_c=10.4,
            temp_mean_7d_c=10.8,
            temp_mean_14d_c=11.2,
            relative_humidity_mean_3d_percent=76.0,
            relative_humidity_mean_7d_percent=73.0,
            mean_temperature_c=10.8,
            min_temperature_c=4.1,
            max_temperature_c=16.3,
            estimated_current_soil_moisture_index=0.58,
            evapotranspiration_7d_mm=9.0,
            dry_days_count_14d=8,
            temperature_trend_c_per_day=-0.35,
        ),
    ),
)


class MockHabitatDataSource:
    """Select the nearest synthetic habitat sample; never treat it as observed data."""

    def get_features(self, location: Location) -> FeatureSnapshot[StaticHabitatFeatures]:
        sample = min(_HABITAT_SAMPLES, key=lambda item: _distance_km(location, item.location))
        distance = _distance_km(location, sample.location)
        return FeatureSnapshot(
            features=sample.features,
            metadata=DataSourceMetadata(
                source_name="synthetic_habitat_v0",
                quality=0.45,
                is_mock=True,
                details={"sample_id": sample.sample_id, "distance_to_sample_km": round(distance, 2)},
            ),
        )


class MockWeatherDataSource:
    """Select the nearest synthetic weather sample for plumbing and tests."""

    def get_features(
        self, location: Location, target_date: date
    ) -> FeatureSnapshot[DynamicWeatherFeatures]:
        sample = min(_WEATHER_SAMPLES, key=lambda item: _distance_km(location, item.location))
        distance = _distance_km(location, sample.location)
        return FeatureSnapshot(
            features=sample.features,
            metadata=DataSourceMetadata(
                source_name="synthetic_weather_v0",
                quality=0.35,
                is_mock=True,
                details={
                    "sample_id": sample.sample_id,
                    "distance_to_sample_km": round(distance, 2),
                    "baseline_date": sample.baseline_date.isoformat(),
                    "requested_date": target_date.isoformat(),
                },
            ),
        )
