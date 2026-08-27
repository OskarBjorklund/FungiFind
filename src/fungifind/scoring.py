"""Transparent rule-based v0 scorer that can later be replaced by an ML implementation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from fungifind.config import SpeciesConfig, TrapezoidPreference, get_species_config
from fungifind.models import (
    DynamicWeatherFeatures,
    FeatureSnapshot,
    HabitatExclusion,
    Location,
    ModelResult,
    Species,
    StaticHabitatFeatures,
)


@dataclass(frozen=True, slots=True)
class _Component:
    score: float | None
    completeness: float
    missing: tuple[str, ...] = ()


class ScoringEngine(Protocol):
    """Stable scoring boundary for a future trained model."""

    def score(
        self,
        species: Species,
        location: Location,
        target_date: date,
        habitat: FeatureSnapshot[StaticHabitatFeatures],
        weather: FeatureSnapshot[DynamicWeatherFeatures],
    ) -> ModelResult: ...


def collect_habitat_exclusions(
    habitat: FeatureSnapshot[StaticHabitatFeatures],
) -> tuple[HabitatExclusion, ...]:
    """Collect every validated exclusion marker without collapsing source provenance."""

    exclusions: list[HabitatExclusion] = []
    seen: set[tuple[str, str | None, str, float | int | None]] = set()
    for source_feature, provenance in habitat.feature_provenance.items():
        if not provenance.semantic_status.startswith("validated"):
            continue
        reason_code = provenance.details.get("habitat_exclusion_code")
        reason_label = provenance.details.get("habitat_exclusion_label")
        if not isinstance(reason_code, str) or not isinstance(reason_label, str):
            continue
        key = (
            provenance.source_name,
            provenance.source_path,
            reason_code,
            provenance.raw_value,
        )
        if key in seen:
            continue
        seen.add(key)
        exclusions.append(
            HabitatExclusion(
                reason_code=reason_code,
                reason_label=reason_label,
                source_feature=source_feature,
                source_name=provenance.source_name,
                source_path=provenance.source_path,
                raw_value=provenance.raw_value,
                semantic_status=provenance.semantic_status,
            )
        )
    return tuple(exclusions)


def _preference_component(
    features: object,
    weights: Mapping[str, float],
    preferences: Mapping[str, TrapezoidPreference],
) -> _Component:
    total_weight = sum(weights.values())
    available_weight = 0.0
    weighted_score = 0.0
    missing: list[str] = []
    for name, weight in weights.items():
        value = getattr(features, name)
        if value is None:
            missing.append(name)
            continue
        available_weight += weight
        weighted_score += weight * preferences[name].score(float(value))
    if available_weight == 0:
        return _Component(score=None, completeness=0.0, missing=tuple(missing))
    return _Component(
        score=weighted_score / available_weight,
        completeness=available_weight / total_weight,
        missing=tuple(missing),
    )


def _tree_component(features: StaticHabitatFeatures, config: SpeciesConfig) -> _Component:
    known_share = 0.0
    weighted_affinity = 0.0
    missing: list[str] = []
    for name, affinity in config.tree_affinities.items():
        share = getattr(features, name)
        if share is None:
            missing.append(name)
            continue
        known_share += share
        weighted_affinity += share * affinity
    if known_share == 0:
        return _Component(score=None, completeness=0.0, missing=tuple(missing))
    return _Component(
        score=weighted_affinity / known_share,
        completeness=min(known_share, 1.0),
        missing=tuple(missing),
    )


def _soil_component(features: StaticHabitatFeatures, config: SpeciesConfig) -> _Component:
    if features.soil_type is None:
        return _Component(score=None, completeness=0.0, missing=("soil_type",))
    return _Component(score=config.soil_affinities[features.soil_type], completeness=1.0)


def _static_wetness_component(
    habitat: FeatureSnapshot[StaticHabitatFeatures],
    config: SpeciesConfig,
) -> _Component:
    wetness_class = habitat.features.static_wetness_class
    provenance = habitat.feature_provenance.get("static_wetness_class")
    semantics_are_validated = (
        provenance is not None
        and provenance.semantic_status.startswith("validated")
        and provenance.interpreted_value == wetness_class
    )
    if wetness_class is None or not semantics_are_validated:
        return _Component(
            score=None,
            completeness=0.0,
            missing=("static_wetness_class",),
        )
    preference = config.static_wetness_preferences.get(wetness_class)
    if preference is None:
        return _Component(
            score=None,
            completeness=0.0,
            missing=("static_wetness_class",),
        )
    return _Component(score=preference, completeness=1.0)


def _season_component(target_date: date, config: SpeciesConfig) -> _Component:
    return _Component(score=config.season_preference.score(target_date.timetuple().tm_yday), completeness=1.0)


def _rain_components(
    features: DynamicWeatherFeatures, config: SpeciesConfig
) -> dict[str, _Component]:
    """Group correlated cumulative windows before fruiting-score weighting."""

    return {
        group_name: _preference_component(
            features,
            window_weights,
            {
                feature_name: config.rainfall_preferences[feature_name]
                for feature_name in window_weights
            },
        )
        for group_name, window_weights in config.rainfall_group_windows.items()
    }


def _weather_completeness(
    weather: FeatureSnapshot[DynamicWeatherFeatures],
) -> dict[str, str]:
    return {
        name: str(provenance.details["coverage_status"])
        for name, provenance in weather.feature_provenance.items()
        if "coverage_status" in provenance.details
    }


def _combine_components(
    components: Mapping[str, _Component], weights: Mapping[str, float]
) -> tuple[float | None, float, tuple[str, ...]]:
    total_weight = sum(weights.values())
    score_weight = 0.0
    weighted_score = 0.0
    completeness = 0.0
    missing: list[str] = []
    for name, weight in weights.items():
        component = components[name]
        completeness += weight * component.completeness
        missing.extend(component.missing)
        if component.score is not None:
            score_weight += weight
            weighted_score += weight * component.score
    score = None if score_weight == 0 else weighted_score / score_weight
    return score, completeness / total_weight, tuple(sorted(set(missing)))


class RuleBasedScoringEngine:
    """Version-zero heuristic. Scores are suitability indices, never probabilities."""

    def score(
        self,
        species: Species,
        location: Location,
        target_date: date,
        habitat: FeatureSnapshot[StaticHabitatFeatures],
        weather: FeatureSnapshot[DynamicWeatherFeatures],
    ) -> ModelResult:
        habitat_exclusions = collect_habitat_exclusions(habitat)
        if habitat_exclusions:
            return ModelResult(
                species=species,
                location=location,
                date=target_date,
                habitat_score=None,
                fruiting_score=None,
                final_score=None,
                confidence=0.0,
                factors={},
                missing_features=(),
                data_sources={
                    "habitat": habitat.metadata.source_name,
                    "weather": weather.metadata.source_name,
                },
                feature_provenance={
                    **habitat.feature_provenance,
                    **weather.feature_provenance,
                },
                score_type="excluded_habitat_no_suitability_index_v0",
                eligibility_status="excluded",
                habitat_exclusions=habitat_exclusions,
                weather_completeness=_weather_completeness(weather),
            )

        config = get_species_config(species)
        habitat_components = {
            "forest": _preference_component(
                habitat.features, config.forest_weights, config.forest_preferences
            ),
            "tree_species": _tree_component(habitat.features, config),
            "soil_moisture": _preference_component(
                habitat.features,
                config.static_moisture_weights,
                config.static_moisture_preferences,
            ),
            "terrain": _preference_component(
                habitat.features, config.terrain_weights, config.terrain_preferences
            ),
            "soil": _soil_component(habitat.features, config),
            "static_wetness": _static_wetness_component(habitat, config),
        }
        rain_components = _rain_components(weather.features, config)
        fruiting_components = {
            **rain_components,
            "temperature": _preference_component(
                weather.features, config.temperature_weights, config.temperature_preferences
            ),
            "relative_humidity": _preference_component(
                weather.features,
                config.relative_humidity_weights,
                config.relative_humidity_preferences,
            ),
            "season": _season_component(target_date, config),
        }

        habitat_weights = dict(config.habitat_component_weights)
        # Unavailable or unvalidated wetness is deliberately score-neutral. Its
        # component and weight are activated only when the adapter supplies a
        # class from an explicit validated mapping.
        if habitat_components["static_wetness"].score is None:
            habitat_weights.pop("static_wetness")
            scored_habitat_components = {
                name: component
                for name, component in habitat_components.items()
                if name != "static_wetness"
            }
        else:
            scored_habitat_components = habitat_components
        habitat_score, habitat_completeness, habitat_missing = _combine_components(
            scored_habitat_components, habitat_weights
        )
        fruiting_score, fruiting_completeness, fruiting_missing = _combine_components(
            fruiting_components, config.fruiting_component_weights
        )

        habitat_weight = config.final_habitat_weight
        fruiting_weight = 1.0 - habitat_weight
        available_final_weight = 0.0
        final_score = 0.0
        if habitat_score is not None:
            final_score += habitat_weight * habitat_score
            available_final_weight += habitat_weight
        if fruiting_score is not None:
            final_score += fruiting_weight * fruiting_score
            available_final_weight += fruiting_weight
        if available_final_weight == 0:
            raise ValueError("Cannot calculate a score: all habitat and weather features are missing")
        final_score /= available_final_weight

        # Confidence is a data-completeness indicator discounted by coarse source quality.
        # It is not statistical uncertainty and mock source qualities intentionally cap it.
        confidence = (
            habitat_weight * habitat_completeness * habitat.metadata.quality
            + fruiting_weight * fruiting_completeness * weather.metadata.quality
        )
        factor_scores = {
            name: round(component.score, 6)
            for name, component in {**habitat_components, **fruiting_components}.items()
            if component.score is not None
        }
        precipitation_score, _, _ = _combine_components(
            rain_components,
            {
                name: config.fruiting_component_weights[name]
                for name in rain_components
            },
        )
        if precipitation_score is not None:
            factor_scores["precipitation"] = round(precipitation_score, 6)

        return ModelResult(
            species=species,
            location=location,
            date=target_date,
            habitat_score=None if habitat_score is None else round(habitat_score, 6),
            fruiting_score=None if fruiting_score is None else round(fruiting_score, 6),
            final_score=round(final_score, 6),
            confidence=round(confidence, 6),
            factors=factor_scores,
            missing_features=tuple(sorted(set(habitat_missing + fruiting_missing))),
            data_sources={
                "habitat": habitat.metadata.source_name,
                "weather": weather.metadata.source_name,
            },
            feature_provenance={
                **habitat.feature_provenance,
                **weather.feature_provenance,
            },
            weather_completeness=_weather_completeness(weather),
        )
