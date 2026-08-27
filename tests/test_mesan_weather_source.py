from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from fungifind.config import get_species_config
from fungifind.data_sources import (
    CoverageStatus,
    MesanHistoryArchive,
    MesanHistoryError,
    MesanWeatherHistoryDataSource,
    MockHabitatDataSource,
    MockWeatherDataSource,
    WeatherDataSource,
)
from fungifind.data_sources.smhi_mesan import (
    MesanGridPoint,
    MesanHourlyAnalysis,
    MesanPointAnalysis,
)
from fungifind.models import (
    DataSourceMetadata,
    DynamicWeatherFeatures,
    FeatureProvenance,
    FeatureSnapshot,
    Location,
    Species,
    StaticHabitatFeatures,
)
from fungifind.service import MushroomScoringService

REQUESTED = Location(59.412, 18.132)
GRID = MesanGridPoint(59.418897, 18.129709)
START = datetime(2026, 7, 28, 12, tzinfo=UTC)


def _populate_archive(path: Path, *, hours: int = 720) -> datetime:
    end = START + timedelta(hours=hours - 1)
    records = tuple(
        MesanHourlyAnalysis(
            time=START + timedelta(hours=index),
            grid_point=GRID,
            air_temperature_c=8.0 + (index % 24) / 4,
            precipitation_1h_mm=0.25,
            relative_humidity_percent=72.0,
            wind_speed_m_s=3.0,
            values={},
            units={
                "air_temperature": "Cel",
                "precipitation_amount_last_1_hours": "mm",
                "relative_humidity": "percent",
                "wind_speed": "m/s",
            },
        )
        for index in range(hours)
    )
    MesanHistoryArchive(path).insert_point_analysis(
        MesanPointAnalysis(
            requested_point=MesanGridPoint(REQUESTED.latitude, REQUESTED.longitude),
            grid_point=GRID,
            reference_time=end,
            created_time=end + timedelta(minutes=30),
            timesteps=records,
            parameters={},
        )
    )
    return end


def _complete_weather(**overrides: float) -> DynamicWeatherFeatures:
    values = {
        "rainfall_1d_mm": 5.0,
        "rainfall_3d_mm": 12.0,
        "rainfall_7d_mm": 25.0,
        "rainfall_14d_mm": 48.0,
        "rainfall_21d_mm": 70.0,
        "rainfall_30d_mm": 95.0,
        "temp_mean_3d_c": 15.0,
        "temp_mean_7d_c": 14.0,
        "temp_mean_14d_c": 13.0,
        "relative_humidity_mean_3d_percent": 80.0,
        "relative_humidity_mean_7d_percent": 78.0,
    }
    values.update(overrides)
    return DynamicWeatherFeatures(**values)


class _WeatherSource:
    def __init__(self, features: DynamicWeatherFeatures, *, quality: float = 1.0) -> None:
        self.features = features
        self.quality = quality

    def get_features(
        self, location: Location, target_date: date
    ) -> FeatureSnapshot[DynamicWeatherFeatures]:
        return FeatureSnapshot(
            self.features,
            DataSourceMetadata("test_weather", quality=self.quality),
        )


class _ExcludedHabitatSource:
    def get_features(self, location: Location) -> FeatureSnapshot[StaticHabitatFeatures]:
        return FeatureSnapshot(
            StaticHabitatFeatures(landcover_class=61, landcover_label="Hav"),
            DataSourceMetadata("test_nmd", quality=1.0),
            {
                "landcover_class": FeatureProvenance(
                    source_name="test_nmd",
                    quality=1.0,
                    is_mock=False,
                    semantic_status="validated_official_mapping",
                    raw_value=61,
                    interpreted_value=61,
                    details={
                        "habitat_exclusion_code": "water",
                        "habitat_exclusion_label": "Vatten enligt NMD",
                    },
                )
            },
        )


def test_full_mesan_coverage_maps_only_verified_aggregates_and_provenance(
    tmp_path: Path,
) -> None:
    database = tmp_path / "history.sqlite"
    target = _populate_archive(database)
    source = MesanWeatherHistoryDataSource(database)

    assert isinstance(source, WeatherDataSource)
    history, resolution = source.get_history_features(REQUESTED, target)
    snapshot = source.get_features(REQUESTED, target.date())

    assert resolution.target_time == target
    assert history.rain_30d_mm.status is CoverageStatus.FULL
    assert snapshot.features.rainfall_1d_mm == pytest.approx(6.0)
    assert snapshot.features.rainfall_30d_mm == pytest.approx(180.0)
    assert snapshot.features.temp_mean_14d_c == pytest.approx(10.875)
    assert snapshot.features.relative_humidity_mean_7d_percent == pytest.approx(72.0)
    assert snapshot.features.days_since_significant_rain is None
    assert snapshot.metadata.is_mock is False

    provenance = snapshot.feature_provenance["rainfall_30d_mm"]
    assert provenance.semantic_status == "validated_full_coverage"
    assert provenance.source_path == str(database.resolve())
    assert provenance.interpreted_value == pytest.approx(180.0)
    assert provenance.details["coverage_status"] == "full"
    assert provenance.details["expected_hours"] == 720
    assert provenance.details["source_products"] == "SMHI_MESAN2G"
    assert provenance.details["grid_distance_m"] > 0


def test_partial_mesan_window_is_none_and_reduces_scoring_completeness(
    tmp_path: Path,
) -> None:
    full_database = tmp_path / "full.sqlite"
    partial_database = tmp_path / "partial.sqlite"
    _populate_archive(full_database)
    _populate_archive(partial_database, hours=48)
    full_source = MesanWeatherHistoryDataSource(full_database)
    partial_source = MesanWeatherHistoryDataSource(partial_database)

    partial = partial_source.get_features(REQUESTED, date(2026, 8, 27))
    assert partial.features.rainfall_1d_mm == pytest.approx(6.0)
    assert partial.features.rainfall_3d_mm is None
    assert partial.feature_provenance["rainfall_3d_mm"].details["coverage_status"] == "partial"
    assert partial.feature_provenance["rainfall_3d_mm"].interpreted_value is None

    full_result = MushroomScoringService(MockHabitatDataSource(), full_source).get_score(
        REQUESTED.latitude,
        REQUESTED.longitude,
        "2026-08-27",
        Species.CANTHARELLUS_CIBARIUS,
    )
    partial_result = MushroomScoringService(
        MockHabitatDataSource(), partial_source
    ).get_score(
        REQUESTED.latitude,
        REQUESTED.longitude,
        "2026-08-27",
        Species.CANTHARELLUS_CIBARIUS,
    )
    assert partial_result.confidence < full_result.confidence
    assert partial_result.weather_completeness["rainfall_3d_mm"] == "partial"
    assert "rainfall_3d_mm" in partial_result.missing_features


def test_location_must_have_a_nearby_backfilled_grid_point(tmp_path: Path) -> None:
    database = tmp_path / "history.sqlite"
    _populate_archive(database)

    with pytest.raises(MesanHistoryError, match="backfill this location first"):
        MesanWeatherHistoryDataSource(database).get_features(
            Location(57.25, 14.60), date(2026, 8, 27)
        )


def test_real_weather_source_replaces_mock_weather_without_feature_mixing(
    tmp_path: Path,
) -> None:
    database = tmp_path / "history.sqlite"
    _populate_archive(database)
    real_source = MesanWeatherHistoryDataSource(database)
    real = real_source.get_features(REQUESTED, date(2026, 8, 27))
    mock = MockWeatherDataSource().get_features(REQUESTED, date(2026, 8, 27))

    assert real.features.rainfall_30d_mm != mock.features.rainfall_30d_mm
    assert real.feature_provenance["rainfall_30d_mm"].is_mock is False
    assert real.metadata.source_name == "smhi_mesan_weather_history_v1"
    result = MushroomScoringService(MockHabitatDataSource(), real_source).get_score(
        REQUESTED.latitude,
        REQUESTED.longitude,
        "2026-08-27",
        Species.CANTHARELLUS_CIBARIUS,
    )
    assert result.data_sources["weather"] == "smhi_mesan_weather_history_v1"
    assert all(not item.is_mock for item in result.feature_provenance.values())


def test_precipitation_windows_are_composed_in_three_correlated_groups() -> None:
    config = get_species_config(Species.CANTHARELLUS_CIBARIUS)
    features = _complete_weather()
    result = MushroomScoringService(
        MockHabitatDataSource(), _WeatherSource(features)
    ).get_score(59.412, 18.132, "2026-08-27", Species.CANTHARELLUS_CIBARIUS)

    recent_expected = (
        0.35 * config.rainfall_preferences["rainfall_1d_mm"].score(5.0)
        + 0.65 * config.rainfall_preferences["rainfall_3d_mm"].score(12.0)
    )
    assert result.factors["recent_rain"] == pytest.approx(recent_expected, abs=1e-6)
    assert {"recent_rain", "medium_term_rain", "background_rain", "precipitation"} <= set(
        result.factors
    )
    assert not any(name.startswith("rainfall_") for name in result.factors)


def test_temperature_and_humidity_curves_are_species_specific_and_bounded() -> None:
    chanterelle_config = get_species_config(Species.CANTHARELLUS_CIBARIUS)
    funnel_config = get_species_config(Species.CRATERELLUS_TUBAEFORMIS)
    assert chanterelle_config.temperature_preferences != funnel_config.temperature_preferences
    assert chanterelle_config.relative_humidity_preferences != (
        funnel_config.relative_humidity_preferences
    )
    assert chanterelle_config.fruiting_component_weights["relative_humidity"] < (
        chanterelle_config.fruiting_component_weights["temperature"]
    )
    assert chanterelle_config.dry_spell_scoring_enabled is False
    assert funnel_config.dry_spell_scoring_enabled is False

    source = _WeatherSource(
        _complete_weather(
            temp_mean_3d_c=20.0,
            temp_mean_7d_c=20.0,
            temp_mean_14d_c=20.0,
            relative_humidity_mean_3d_percent=55.0,
            relative_humidity_mean_7d_percent=55.0,
        )
    )
    service = MushroomScoringService(MockHabitatDataSource(), source)
    chanterelle = service.get_score(
        59.412, 18.132, "2026-08-27", Species.CANTHARELLUS_CIBARIUS
    )
    funnel = service.get_score(
        59.412, 18.132, "2026-08-27", Species.CRATERELLUS_TUBAEFORMIS
    )
    assert chanterelle.factors["temperature"] != funnel.factors["temperature"]
    assert chanterelle.factors["relative_humidity"] != funnel.factors["relative_humidity"]
    for result in (chanterelle, funnel):
        assert 0 <= result.fruiting_score <= 1
        assert 0 <= result.final_score <= 1


def test_missing_weather_features_are_reported_without_mock_fallback() -> None:
    result = MushroomScoringService(
        MockHabitatDataSource(), _WeatherSource(DynamicWeatherFeatures())
    ).get_score(59.412, 18.132, "2026-08-27", Species.CANTHARELLUS_CIBARIUS)

    assert "rainfall_1d_mm" in result.missing_features
    assert "temp_mean_14d_c" in result.missing_features
    assert "relative_humidity_mean_7d_percent" in result.missing_features
    assert result.factors["season"] == pytest.approx(1.0)
    assert not {
        "recent_rain",
        "medium_term_rain",
        "background_rain",
        "precipitation",
        "temperature",
        "relative_humidity",
    } & set(result.factors)
    assert result.confidence < 0.5


def test_excluded_habitat_remains_excluded_with_complete_weather() -> None:
    result = MushroomScoringService(
        _ExcludedHabitatSource(), _WeatherSource(_complete_weather())
    ).get_score(59.412, 18.132, "2026-08-27", Species.CANTHARELLUS_CIBARIUS)

    assert result.eligibility_status == "excluded"
    assert result.habitat_score is None
    assert result.fruiting_score is None
    assert result.final_score is None
