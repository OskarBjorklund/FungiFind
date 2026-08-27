"""Parallel experimental fruiting model using current-soil-moisture v1."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date

from fungifind.config import TrapezoidPreference, get_species_config
from fungifind.models import (
    CurrentSoilMoistureResult,
    DynamicWeatherFeatures,
    FruitingV2Breakdown,
    ModelResult,
    Species,
)

FRUITING_V2_COMPONENTS = frozenset(
    {"current_soil_moisture", "temperature", "season", "recent_rain_trigger"}
)


def _weighted_preference(
    features: DynamicWeatherFeatures,
    weights: Mapping[str, float],
    preferences: Mapping[str, TrapezoidPreference],
) -> tuple[float | None, tuple[str, ...]]:
    missing = tuple(name for name in weights if getattr(features, name) is None)
    if missing:
        return None, missing
    return (
        sum(
            weight * preferences[name].score(float(getattr(features, name)))
            for name, weight in weights.items()
        )
        / sum(weights.values()),
        (),
    )


def _moisture_is_available(result: CurrentSoilMoistureResult) -> bool:
    usage = result.used_inputs
    return (
        result.status in {"estimated_complete", "estimated_optional_inputs_missing"}
        and result.estimated_current_soil_moisture is not None
        and usage.used_static_wetness
        and usage.used_rain_history
        and usage.used_temperature
        and usage.used_relative_humidity
    )


def _validate_component_weights(weights: Mapping[str, float]) -> None:
    if set(weights) != FRUITING_V2_COMPONENTS:
        raise ValueError("fruiting v2 must define exactly its four top-level components")
    if any(not math.isfinite(value) or value < 0 for value in weights.values()):
        raise ValueError("fruiting v2 component weights must be finite and non-negative")
    if not math.isclose(sum(weights.values()), 1.0, abs_tol=1e-9):
        raise ValueError("fruiting v2 component weights must sum to 1")


class ExperimentalFruitingV2Engine:
    """Evaluate v2 alongside, but never instead of, the production v1 scores."""

    def evaluate(
        self,
        *,
        species: Species,
        target_date: date,
        weather: DynamicWeatherFeatures,
        moisture: CurrentSoilMoistureResult,
        production_result: ModelResult,
    ) -> FruitingV2Breakdown:
        config = get_species_config(species)
        weights = config.fruiting_v2_component_weights
        _validate_component_weights(weights)

        common = {
            "current_soil_moisture": moisture.estimated_current_soil_moisture,
            "moisture_confidence": moisture.confidence,
            "moisture_completeness": moisture.completeness,
            "component_weights": weights,
        }
        if production_result.habitat_exclusions or production_result.eligibility_status == "excluded":
            return FruitingV2Breakdown(
                status="excluded_habitat",
                moisture_preference_score=None,
                recent_rain_trigger=None,
                temperature_suitability=None,
                season_suitability=None,
                fruiting_score_v2=None,
                final_score_v2=None,
                missing_inputs=(),
                **common,
            )

        if not _moisture_is_available(moisture):
            return FruitingV2Breakdown(
                status="insufficient_moisture",
                moisture_preference_score=None,
                recent_rain_trigger=None,
                temperature_suitability=None,
                season_suitability=None,
                fruiting_score_v2=None,
                final_score_v2=None,
                missing_inputs=moisture.missing_inputs,
                **common,
            )

        recent_weights = config.rainfall_group_windows["recent_rain"]
        recent_rain_trigger, recent_missing = _weighted_preference(
            weather,
            recent_weights,
            {name: config.rainfall_preferences[name] for name in recent_weights},
        )
        temperature_suitability, temperature_missing = _weighted_preference(
            weather,
            config.temperature_weights,
            config.temperature_preferences,
        )
        season_suitability = config.season_preference.score(
            target_date.timetuple().tm_yday
        )
        moisture_value = moisture.estimated_current_soil_moisture
        assert moisture_value is not None
        moisture_preference = config.current_soil_moisture_preference.score(
            moisture_value
        )

        missing = tuple(sorted(set(recent_missing + temperature_missing)))
        if recent_rain_trigger is None or temperature_suitability is None:
            return FruitingV2Breakdown(
                status="insufficient_weather",
                moisture_preference_score=round(moisture_preference, 6),
                recent_rain_trigger=None,
                temperature_suitability=None,
                season_suitability=round(season_suitability, 6),
                fruiting_score_v2=None,
                final_score_v2=None,
                missing_inputs=missing,
                **common,
            )

        component_scores = {
            "current_soil_moisture": moisture_preference,
            "temperature": temperature_suitability,
            "season": season_suitability,
            "recent_rain_trigger": recent_rain_trigger,
        }
        fruiting_score_v2 = sum(
            weights[name] * score for name, score in component_scores.items()
        )
        if production_result.habitat_score is None:
            final_score_v2 = None
            status = "available_fruiting_only"
            missing = ("habitat_score",)
        else:
            final_score_v2 = (
                config.final_habitat_weight * production_result.habitat_score
                + (1.0 - config.final_habitat_weight) * fruiting_score_v2
            )
            status = "available"

        return FruitingV2Breakdown(
            status=status,
            moisture_preference_score=round(moisture_preference, 6),
            recent_rain_trigger=round(recent_rain_trigger, 6),
            temperature_suitability=round(temperature_suitability, 6),
            season_suitability=round(season_suitability, 6),
            fruiting_score_v2=round(fruiting_score_v2, 6),
            final_score_v2=(
                None if final_score_v2 is None else round(final_score_v2, 6)
            ),
            missing_inputs=missing,
            **common,
        )
