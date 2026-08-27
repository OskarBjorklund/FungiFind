from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from fungifind.config import get_species_config
from fungifind.fruiting_v2 import ExperimentalFruitingV2Engine
from fungifind.models import (
    CurrentSoilMoistureResult,
    DataSourceMetadata,
    DynamicWeatherFeatures,
    FeatureProvenance,
    FeatureSnapshot,
    HabitatExclusion,
    Location,
    ModelResult,
    MoistureInputUsage,
    Species,
    StaticHabitatFeatures,
)
from fungifind.moisture import CurrentSoilMoistureEstimator
from fungifind.scoring import RuleBasedScoringEngine
from fungifind.service import MushroomScoringService

TARGET_DATE = date(2026, 9, 10)
LOCATION = Location(59.42, 18.13)


def _provenance(value: float | None, status: str = "validated_test_input") -> FeatureProvenance:
    return FeatureProvenance(
        source_name="validated_test_source",
        quality=1.0,
        is_mock=False,
        semantic_status=status,
        raw_value=value,
        interpreted_value=value,
    )


def _habitat(
    *, wetness_class: int = 2, soil_group: str = "moraine", slope_degrees: float = 5.0
) -> FeatureSnapshot[StaticHabitatFeatures]:
    return FeatureSnapshot(
        features=StaticHabitatFeatures(
            static_wetness_class=wetness_class,
            soil_group=soil_group,
            slope_degrees=slope_degrees,
            spruce_fraction=0.6,
            pine_fraction=0.2,
            birch_fraction=0.2,
        ),
        metadata=DataSourceMetadata("validated_habitat", quality=1.0),
        feature_provenance={
            "static_wetness_class": _provenance(float(wetness_class)),
            "soil_group": _provenance(
                None, status="derived_from_validated_test_mapping"
            ),
            "slope_degrees": _provenance(
                slope_degrees, status="derived_horn_validated_test_input"
            ),
        },
    )


def _weather(**overrides: float | None) -> FeatureSnapshot[DynamicWeatherFeatures]:
    values = {
        "rainfall_1d_mm": 2.0,
        "rainfall_3d_mm": 8.0,
        "rainfall_7d_mm": 20.0,
        "rainfall_14d_mm": 40.0,
        "rainfall_21d_mm": 55.0,
        "rainfall_30d_mm": 70.0,
        "temp_mean_3d_c": 14.0,
        "temp_mean_7d_c": 14.0,
        "temp_mean_14d_c": 14.0,
        "relative_humidity_mean_3d_percent": 78.0,
        "relative_humidity_mean_7d_percent": 76.0,
        **overrides,
    }
    return FeatureSnapshot(
        features=DynamicWeatherFeatures(**values),
        metadata=DataSourceMetadata("validated_weather", quality=1.0),
        feature_provenance={
            name: _provenance(value) for name, value in values.items() if value is not None
        },
    )


def _available_moisture(value: float) -> CurrentSoilMoistureResult:
    return CurrentSoilMoistureResult(
        baseline_wetness=0.55,
        rain_recharge=0.5,
        drying_pressure=0.2,
        soil_retention=0.55,
        terrain_retention=0.8,
        estimated_current_soil_moisture=value,
        confidence=0.95,
        completeness=1.0,
        status="estimated_complete",
        used_inputs=MoistureInputUsage(
            used_static_wetness=True,
            used_rain_history=True,
            used_temperature=True,
            used_relative_humidity=True,
            used_soil=True,
            used_slope=True,
        ),
    )


def _production_result(
    species: Species = Species.CANTHARELLUS_CIBARIUS,
) -> ModelResult:
    return ModelResult(
        species=species,
        location=LOCATION,
        date=TARGET_DATE,
        habitat_score=0.7,
        fruiting_score=0.6,
        final_score=0.66,
        confidence=1.0,
        factors={},
        missing_features=(),
        data_sources={},
    )


def _evaluate(
    *,
    species: Species = Species.CANTHARELLUS_CIBARIUS,
    moisture: CurrentSoilMoistureResult | None = None,
    weather: DynamicWeatherFeatures | None = None,
    target_date: date = TARGET_DATE,
    production_result: ModelResult | None = None,
):
    return ExperimentalFruitingV2Engine().evaluate(
        species=species,
        target_date=target_date,
        weather=weather or _weather().features,
        moisture=moisture or _available_moisture(0.60),
        production_result=production_result or _production_result(species),
    )


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


def test_service_preserves_every_production_score_and_factor() -> None:
    habitat = _habitat()
    weather = _weather()
    production = RuleBasedScoringEngine().score(
        Species.CANTHARELLUS_CIBARIUS,
        LOCATION,
        TARGET_DATE,
        habitat,
        weather,
    )
    integrated = MushroomScoringService(
        _HabitatSource(habitat), _WeatherSource(weather)
    ).get_score(
        LOCATION.latitude,
        LOCATION.longitude,
        TARGET_DATE,
        Species.CANTHARELLUS_CIBARIUS,
    )

    assert integrated.habitat_score == production.habitat_score
    assert integrated.fruiting_score == production.fruiting_score
    assert integrated.final_score == production.final_score
    assert integrated.confidence == production.confidence
    assert integrated.factors == production.factors
    assert integrated.fruiting_score_v2 is not None
    assert integrated.final_score_v2 is not None


@pytest.mark.parametrize("species", list(Species))
def test_v2_is_bounded_and_uses_exact_configured_formula(species: Species) -> None:
    result = _evaluate(species=species)
    config = get_species_config(species)
    expected = (
        config.fruiting_v2_component_weights["current_soil_moisture"]
        * result.moisture_preference_score
        + config.fruiting_v2_component_weights["temperature"]
        * result.temperature_suitability
        + config.fruiting_v2_component_weights["season"] * result.season_suitability
        + config.fruiting_v2_component_weights["recent_rain_trigger"]
        * result.recent_rain_trigger
    )

    assert result.status == "available"
    assert result.fruiting_score_v2 == pytest.approx(expected, abs=1e-6)
    assert 0.0 <= result.fruiting_score_v2 <= 1.0
    assert 0.0 <= result.final_score_v2 <= 1.0


def test_moisture_preference_is_species_specific_and_not_monotonic_to_one() -> None:
    chanterelle_mid = _evaluate(moisture=_available_moisture(0.60))
    funnel_mid = _evaluate(
        species=Species.CRATERELLUS_TUBAEFORMIS,
        moisture=_available_moisture(0.60),
    )
    chanterelle_saturated = _evaluate(moisture=_available_moisture(1.0))
    funnel_saturated = _evaluate(
        species=Species.CRATERELLUS_TUBAEFORMIS,
        moisture=_available_moisture(1.0),
    )

    assert chanterelle_mid.moisture_preference_score == 1.0
    assert funnel_mid.moisture_preference_score == 1.0
    assert chanterelle_saturated.moisture_preference_score == 0.0
    assert funnel_saturated.moisture_preference_score == 0.0
    assert (
        get_species_config(Species.CANTHARELLUS_CIBARIUS)
        .current_soil_moisture_preference
        != get_species_config(Species.CRATERELLUS_TUBAEFORMIS)
        .current_soil_moisture_preference
    )


def test_recent_rain_trigger_is_small_separate_component() -> None:
    dry = _evaluate(
        weather=_weather(rainfall_1d_mm=0.0, rainfall_3d_mm=0.0).features
    )
    recent = _evaluate(
        weather=_weather(rainfall_1d_mm=5.0, rainfall_3d_mm=12.0).features
    )

    assert dry.recent_rain_trigger == 0.0
    assert recent.recent_rain_trigger > dry.recent_rain_trigger
    assert recent.fruiting_score_v2 > dry.fruiting_score_v2
    assert recent.component_weights["recent_rain_trigger"] == 0.10
    assert recent.component_weights["recent_rain_trigger"] < recent.component_weights[
        "current_soil_moisture"
    ]


def test_medium_background_rain_and_rh_are_not_top_level_components() -> None:
    baseline = _evaluate()
    changed_indirect_inputs = _evaluate(
        weather=_weather(
            rainfall_7d_mm=200.0,
            rainfall_14d_mm=300.0,
            rainfall_21d_mm=400.0,
            rainfall_30d_mm=500.0,
            relative_humidity_mean_3d_percent=20.0,
            relative_humidity_mean_7d_percent=20.0,
        ).features
    )

    assert set(baseline.component_weights) == {
        "current_soil_moisture",
        "temperature",
        "season",
        "recent_rain_trigger",
    }
    assert changed_indirect_inputs == baseline


def test_temperature_and_season_remain_separate_biological_components() -> None:
    suitable = _evaluate()
    unsuitable_temperature = _evaluate(
        weather=_weather(
            temp_mean_3d_c=30.0,
            temp_mean_7d_c=30.0,
            temp_mean_14d_c=30.0,
        ).features
    )
    off_season = _evaluate(target_date=date(2026, 2, 1))

    assert suitable.temperature_suitability > unsuitable_temperature.temperature_suitability
    assert suitable.fruiting_score_v2 > unsuitable_temperature.fruiting_score_v2
    assert suitable.season_suitability > off_season.season_suitability
    assert suitable.fruiting_score_v2 > off_season.fruiting_score_v2


def test_exclusion_forces_both_experimental_scores_to_none() -> None:
    exclusion = HabitatExclusion(
        reason_code="open_water",
        reason_label="Open water",
        source_feature="landcover_class",
        source_name="validated_test_source",
        source_path=None,
        raw_value=62,
        semantic_status="validated_test_input",
    )
    excluded = replace(
        _production_result(),
        habitat_score=None,
        fruiting_score=None,
        final_score=None,
        eligibility_status="excluded",
        habitat_exclusions=(exclusion,),
    )
    result = _evaluate(production_result=excluded)

    assert result.status == "excluded_habitat"
    assert result.fruiting_score_v2 is None
    assert result.final_score_v2 is None


def test_missing_moisture_or_partial_weather_is_explicitly_insufficient() -> None:
    insufficient_moisture = replace(
        _available_moisture(0.6),
        estimated_current_soil_moisture=None,
        status="insufficient_central_inputs",
        missing_inputs=("rainfall_30d_mm",),
    )
    missing = _evaluate(moisture=insufficient_moisture)
    partial = _evaluate(weather=_weather(rainfall_3d_mm=None).features)

    assert missing.status == "insufficient_moisture"
    assert missing.fruiting_score_v2 is None
    assert "rainfall_30d_mm" in missing.missing_inputs
    assert partial.status == "insufficient_weather"
    assert partial.fruiting_score_v2 is None


def _scenario(
    rain: dict[str, float], species: Species = Species.CANTHARELLUS_CIBARIUS
):
    habitat = _habitat(wetness_class=2, soil_group="fine_mineral", slope_degrees=12.0)
    weather = _weather(**rain)
    moisture = CurrentSoilMoistureEstimator().estimate(habitat, weather)
    production = RuleBasedScoringEngine().score(
        species, LOCATION, TARGET_DATE, habitat, weather
    )
    return ExperimentalFruitingV2Engine().evaluate(
        species=species,
        target_date=TARGET_DATE,
        weather=weather.features,
        moisture=moisture,
        production_result=production,
    )


def test_requested_sensitivity_scenarios_have_qualitatively_sane_ordering() -> None:
    scenario_a = _scenario(
        {
            **{
                name: 0.0
                for name in (
                    "rainfall_1d_mm",
                    "rainfall_3d_mm",
                    "rainfall_7d_mm",
                    "rainfall_14d_mm",
                    "rainfall_21d_mm",
                    "rainfall_30d_mm",
                )
            },
            "temp_mean_3d_c": 25.0,
            "temp_mean_7d_c": 25.0,
            "temp_mean_14d_c": 25.0,
            "relative_humidity_mean_3d_percent": 40.0,
            "relative_humidity_mean_7d_percent": 40.0,
        }
    )
    scenario_b = _scenario(
        {
            "rainfall_1d_mm": 18.0,
            "rainfall_3d_mm": 18.0,
            "rainfall_7d_mm": 18.0,
            "rainfall_14d_mm": 18.0,
            "rainfall_21d_mm": 18.0,
            "rainfall_30d_mm": 18.0,
            "temp_mean_3d_c": 25.0,
            "temp_mean_7d_c": 25.0,
            "temp_mean_14d_c": 25.0,
            "relative_humidity_mean_3d_percent": 40.0,
            "relative_humidity_mean_7d_percent": 40.0,
        }
    )
    scenario_c = _scenario(
        {
            "rainfall_1d_mm": 0.0,
            "rainfall_3d_mm": 0.0,
            "rainfall_7d_mm": 12.0,
            "rainfall_14d_mm": 45.0,
            "rainfall_21d_mm": 80.0,
            "rainfall_30d_mm": 130.0,
        }
    )
    scenario_d = _scenario(
        {
            "rainfall_1d_mm": 6.0,
            "rainfall_3d_mm": 16.0,
            "rainfall_7d_mm": 28.0,
            "rainfall_14d_mm": 55.0,
            "rainfall_21d_mm": 90.0,
            "rainfall_30d_mm": 140.0,
        }
    )
    scenario_e = _scenario(
        {
            "rainfall_1d_mm": 80.0,
            "rainfall_3d_mm": 150.0,
            "rainfall_7d_mm": 250.0,
            "rainfall_14d_mm": 400.0,
            "rainfall_21d_mm": 600.0,
            "rainfall_30d_mm": 800.0,
            "relative_humidity_mean_3d_percent": 95.0,
            "relative_humidity_mean_7d_percent": 95.0,
        }
    )

    assert scenario_b.fruiting_score_v2 > scenario_a.fruiting_score_v2
    assert scenario_b.fruiting_score_v2 < 1.0
    assert scenario_c.fruiting_score_v2 > scenario_a.fruiting_score_v2
    assert scenario_d.fruiting_score_v2 >= scenario_c.fruiting_score_v2
    assert scenario_e.moisture_preference_score < scenario_d.moisture_preference_score
    assert scenario_e.fruiting_score_v2 < scenario_d.fruiting_score_v2


def test_v2_output_is_deterministic() -> None:
    assert _evaluate() == _evaluate()
