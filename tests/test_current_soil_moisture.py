from __future__ import annotations

from dataclasses import astuple, replace
from datetime import date

import pytest

from fungifind.models import (
    DataSourceMetadata,
    DynamicWeatherFeatures,
    FeatureProvenance,
    FeatureSnapshot,
    Location,
    Species,
    StaticHabitatFeatures,
)
from fungifind.moisture import CurrentSoilMoistureConfig, CurrentSoilMoistureEstimator
from fungifind.scoring import RuleBasedScoringEngine
from fungifind.service import MushroomScoringService

RAIN = {
    "rainfall_1d_mm": 2.0,
    "rainfall_3d_mm": 8.0,
    "rainfall_7d_mm": 20.0,
    "rainfall_14d_mm": 40.0,
    "rainfall_21d_mm": 55.0,
    "rainfall_30d_mm": 70.0,
}
TEMPERATURE = {
    "temp_mean_3d_c": 15.0,
    "temp_mean_7d_c": 14.0,
    "temp_mean_14d_c": 13.0,
}
HUMIDITY = {
    "relative_humidity_mean_3d_percent": 75.0,
    "relative_humidity_mean_7d_percent": 72.0,
}


def _provenance(
    name: str,
    value: float | None,
    *,
    quality: float = 0.95,
    status: str = "validated_test_input",
) -> FeatureProvenance:
    return FeatureProvenance(
        source_name="validated_test_source",
        quality=quality,
        is_mock=False,
        semantic_status=status,
        raw_value=value,
        interpreted_value=value,
    )


def _habitat(
    *,
    wetness_class: int | None = 2,
    soil_group: str | None = "moraine",
    slope_degrees: float | None = 5.0,
    wetness_status: str = "validated_test_input",
    exclusion: bool = False,
) -> FeatureSnapshot[StaticHabitatFeatures]:
    provenance: dict[str, FeatureProvenance] = {}
    if wetness_class is not None:
        wetness_provenance = _provenance(
            "static_wetness_class",
            wetness_class,
            quality=0.98,
            status=wetness_status,
        )
        if exclusion:
            wetness_provenance = replace(
                wetness_provenance,
                details={
                    "habitat_exclusion_code": "open_water",
                    "habitat_exclusion_label": "Open water",
                },
            )
        provenance["static_wetness_class"] = wetness_provenance
    if soil_group is not None:
        provenance["soil_group"] = _provenance(
            "soil_group", None, status="derived_from_validated_test_mapping"
        )
    if slope_degrees is not None:
        provenance["slope_degrees"] = _provenance(
            "slope_degrees",
            slope_degrees,
            status="derived_horn_validated_test_input",
        )
    return FeatureSnapshot(
        features=StaticHabitatFeatures(
            static_wetness_class=wetness_class,
            soil_group=soil_group,
            slope_degrees=slope_degrees,
        ),
        metadata=DataSourceMetadata("validated_habitat", quality=0.95),
        feature_provenance=provenance,
    )


def _weather(**overrides: float | None) -> FeatureSnapshot[DynamicWeatherFeatures]:
    values = {**RAIN, **TEMPERATURE, **HUMIDITY, **overrides}
    return FeatureSnapshot(
        features=DynamicWeatherFeatures(**values),
        metadata=DataSourceMetadata("validated_weather", quality=0.95),
        feature_provenance={
            name: _provenance(name, value) for name, value in values.items() if value is not None
        },
    )


def _estimate(
    habitat: FeatureSnapshot[StaticHabitatFeatures] | None = None,
    weather: FeatureSnapshot[DynamicWeatherFeatures] | None = None,
    config: CurrentSoilMoistureConfig | None = None,
):
    return CurrentSoilMoistureEstimator(config).estimate(
        habitat or _habitat(), weather or _weather()
    )


def test_complete_estimate_exposes_normalized_components_usage_and_confidence() -> None:
    result = _estimate()

    assert result.status == "estimated_complete"
    for value in (
        result.baseline_wetness,
        result.rain_recharge,
        result.drying_pressure,
        result.soil_retention,
        result.terrain_retention,
        result.estimated_current_soil_moisture,
    ):
        assert value is not None
        assert 0.0 <= value <= 1.0
    assert result.baseline_wetness == 0.55
    assert result.soil_retention == 0.55
    assert result.terrain_retention == pytest.approx(0.833333, abs=1e-6)
    assert result.completeness == 1.0
    assert result.confidence == 0.959
    assert all(astuple(result.used_inputs))
    assert result.missing_inputs == ()
    assert result.input_provenance["rain_history"] == "validated_test_source"


def test_more_recent_rain_cannot_reduce_moisture() -> None:
    baseline = _estimate()
    wetter = _estimate(
        weather=_weather(
            rainfall_1d_mm=8.0,
            rainfall_3d_mm=20.0,
            rainfall_7d_mm=32.0,
            rainfall_14d_mm=52.0,
            rainfall_21d_mm=67.0,
            rainfall_30d_mm=82.0,
        )
    )

    assert wetter.rain_recharge > baseline.rain_recharge
    assert wetter.drying_pressure < baseline.drying_pressure
    assert (
        wetter.estimated_current_soil_moisture
        > baseline.estimated_current_soil_moisture
    )


def test_hotter_and_drier_weather_cannot_increase_moisture() -> None:
    baseline = _estimate()
    hotter_and_drier = _estimate(
        weather=_weather(
            temp_mean_3d_c=25.0,
            temp_mean_7d_c=24.0,
            temp_mean_14d_c=23.0,
            relative_humidity_mean_3d_percent=45.0,
            relative_humidity_mean_7d_percent=48.0,
        )
    )

    assert hotter_and_drier.drying_pressure > baseline.drying_pressure
    assert (
        hotter_and_drier.estimated_current_soil_moisture
        < baseline.estimated_current_soil_moisture
    )


def test_peat_retains_more_moisture_than_coarse_mineral_soil() -> None:
    peat = _estimate(habitat=_habitat(soil_group="organic_peat"))
    coarse = _estimate(habitat=_habitat(soil_group="coarse_mineral"))

    assert peat.soil_retention == 0.95
    assert coarse.soil_retention == 0.25
    assert peat.estimated_current_soil_moisture > coarse.estimated_current_soil_moisture


def test_steeper_slope_cannot_increase_moisture() -> None:
    flat = _estimate(habitat=_habitat(slope_degrees=0.0))
    steep = _estimate(habitat=_habitat(slope_degrees=30.0))

    assert flat.terrain_retention == 1.0
    assert steep.terrain_retention == 0.0
    assert flat.estimated_current_soil_moisture > steep.estimated_current_soil_moisture


def test_rain_response_saturates_and_final_estimate_is_clamped() -> None:
    saturated = _estimate(weather=_weather(**{name: 10_000.0 for name in RAIN}))
    more_saturated = _estimate(weather=_weather(**{name: 100_000.0 for name in RAIN}))

    assert saturated.rain_recharge == pytest.approx(1.0, abs=1e-6)
    assert more_saturated.rain_recharge == saturated.rain_recharge

    wet_config = replace(
        CurrentSoilMoistureConfig(),
        static_wetness_baseline={1: 0.90, 2: 0.90, 3: 0.90},
        rain_recharge_effect=2.0,
        drying_pressure_effect=0.0,
    )
    dry_config = replace(
        CurrentSoilMoistureConfig(),
        static_wetness_baseline={1: 0.0, 2: 0.0, 3: 0.0},
        rain_recharge_effect=0.0,
        drying_pressure_effect=2.0,
    )
    clamped_wet = _estimate(
        weather=_weather(**{name: 10_000.0 for name in RAIN}), config=wet_config
    )
    clamped_dry = _estimate(
        weather=_weather(
            **{name: 0.0 for name in RAIN},
            temp_mean_3d_c=40.0,
            temp_mean_7d_c=40.0,
            temp_mean_14d_c=40.0,
            relative_humidity_mean_3d_percent=0.0,
            relative_humidity_mean_7d_percent=0.0,
        ),
        config=dry_config,
    )
    assert clamped_wet.estimated_current_soil_moisture == 1.0
    assert clamped_dry.estimated_current_soil_moisture == 0.0


def test_optional_soil_and_slope_do_not_block_estimate() -> None:
    result = _estimate(habitat=_habitat(soil_group=None, slope_degrees=None))

    assert result.status == "estimated_optional_inputs_missing"
    assert result.estimated_current_soil_moisture is not None
    assert result.soil_retention is None
    assert result.terrain_retention is None
    assert result.completeness == 0.89
    assert result.confidence == 0.8545
    assert result.used_inputs.used_soil is False
    assert result.used_inputs.used_slope is False


def test_missing_or_unvalidated_central_input_is_insufficient() -> None:
    missing_weather = _estimate(weather=_weather(rainfall_30d_mm=None))
    unvalidated_static = _estimate(
        habitat=_habitat(wetness_status="raw_semantics_unvalidated")
    )

    assert missing_weather.status == "insufficient_central_inputs"
    assert missing_weather.estimated_current_soil_moisture is None
    assert "rainfall_30d_mm" in missing_weather.missing_inputs
    assert unvalidated_static.estimated_current_soil_moisture is None
    assert unvalidated_static.used_inputs.used_static_wetness is False


@pytest.mark.parametrize(
    ("habitat", "expected_source"),
    [
        (_habitat(wetness_class=4, exclusion=True), "validated_test_source"),
        (_habitat(soil_group="water"), "validated_test_source"),
        (_habitat(soil_group="ice"), "validated_test_source"),
    ],
)
def test_excluded_habitats_do_not_get_regular_moisture_estimate(
    habitat: FeatureSnapshot[StaticHabitatFeatures], expected_source: str
) -> None:
    result = _estimate(habitat=habitat)

    assert result.status == "excluded_habitat"
    assert result.estimated_current_soil_moisture is None
    assert expected_source in result.input_provenance["habitat_exclusion"]


class _HabitatSource:
    def __init__(self, snapshot: FeatureSnapshot[StaticHabitatFeatures]) -> None:
        self.snapshot = snapshot

    def get_features(self, location: Location) -> FeatureSnapshot[StaticHabitatFeatures]:
        return self.snapshot


class _WeatherSource:
    def __init__(self, snapshot: FeatureSnapshot[DynamicWeatherFeatures]) -> None:
        self.snapshot = snapshot

    def get_features(
        self, location: Location, target_date: date
    ) -> FeatureSnapshot[DynamicWeatherFeatures]:
        return self.snapshot


def test_service_attaches_moisture_without_changing_any_scoring_output() -> None:
    habitat = _habitat()
    weather = _weather()
    location = Location(59.42, 18.13)
    target_date = date(2026, 8, 27)
    scoring_only = RuleBasedScoringEngine().score(
        Species.CANTHARELLUS_CIBARIUS,
        location,
        target_date,
        habitat,
        weather,
    )
    integrated = MushroomScoringService(
        _HabitatSource(habitat), _WeatherSource(weather)
    ).get_score(
        location.latitude,
        location.longitude,
        target_date,
        Species.CANTHARELLUS_CIBARIUS,
    )

    assert integrated.habitat_score == scoring_only.habitat_score
    assert integrated.fruiting_score == scoring_only.fruiting_score
    assert integrated.final_score == scoring_only.final_score
    assert integrated.confidence == scoring_only.confidence
    assert integrated.factors == scoring_only.factors
    assert integrated.estimated_current_soil_moisture is not None
    assert "estimated_current_soil_moisture" not in integrated.factors
