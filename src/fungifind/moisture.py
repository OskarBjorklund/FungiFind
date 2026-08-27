"""Transparent heuristic estimate of current soil moisture.

This module deliberately has no dependency on species configuration or the
rule-based suitability scorer. It consumes only validated feature provenance
and returns an independently auditable result.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from fungifind.models import (
    CurrentSoilMoistureResult,
    DynamicWeatherFeatures,
    FeatureProvenance,
    FeatureSnapshot,
    MoistureInputUsage,
    StaticHabitatFeatures,
)

RAIN_FIELDS = (
    "rainfall_1d_mm",
    "rainfall_3d_mm",
    "rainfall_7d_mm",
    "rainfall_14d_mm",
    "rainfall_21d_mm",
    "rainfall_30d_mm",
)
TEMPERATURE_FIELDS = ("temp_mean_3d_c", "temp_mean_7d_c", "temp_mean_14d_c")
RELATIVE_HUMIDITY_FIELDS = (
    "relative_humidity_mean_3d_percent",
    "relative_humidity_mean_7d_percent",
)


def _static_wetness_baseline() -> Mapping[int, float]:
    # Preliminary heuristic assumptions, not field-calibrated values.
    return {1: 0.30, 2: 0.55, 3: 0.80}


def _rain_saturation_scales() -> Mapping[str, float]:
    # Millimetres at which 1 - exp(-rain / scale) reaches about 0.63.
    return {
        "rainfall_1d_mm": 8.0,
        "rainfall_3d_mm": 15.0,
        "rainfall_7d_mm": 28.0,
        "rainfall_14d_mm": 48.0,
        "rainfall_21d_mm": 70.0,
        "rainfall_30d_mm": 95.0,
    }


def _rain_window_weights() -> Mapping[str, Mapping[str, float]]:
    return {
        "recent": {"rainfall_1d_mm": 0.40, "rainfall_3d_mm": 0.60},
        "medium": {"rainfall_7d_mm": 0.45, "rainfall_14d_mm": 0.55},
        "background": {"rainfall_21d_mm": 0.45, "rainfall_30d_mm": 0.55},
    }


def _rain_group_weights() -> Mapping[str, float]:
    return {"recent": 0.50, "medium": 0.30, "background": 0.20}


def _temperature_window_weights() -> Mapping[str, float]:
    return {"temp_mean_3d_c": 0.50, "temp_mean_7d_c": 0.30, "temp_mean_14d_c": 0.20}


def _relative_humidity_window_weights() -> Mapping[str, float]:
    return {
        "relative_humidity_mean_3d_percent": 0.60,
        "relative_humidity_mean_7d_percent": 0.40,
    }


def _drying_driver_weights() -> Mapping[str, float]:
    return {"temperature": 0.55, "relative_humidity": 0.45}


def _soil_retention() -> Mapping[str, float]:
    # Preliminary heuristic assumptions, not field-calibrated values.
    return {
        "moraine": 0.55,
        "organic_peat": 0.95,
        "coarse_mineral": 0.25,
        "fine_mineral": 0.75,
        "bedrock_or_thin_soil": 0.15,
        "anthropogenic_fill": 0.40,
        "other": 0.50,
    }


def _input_completeness_weights() -> Mapping[str, float]:
    return {
        "static_wetness": 0.30,
        "rain_history": 0.35,
        "temperature": 0.12,
        "relative_humidity": 0.12,
        "soil": 0.08,
        "slope": 0.03,
    }


@dataclass(frozen=True, slots=True)
class CurrentSoilMoistureConfig:
    """All preliminary v1 heuristic assumptions in one configurable object."""

    static_wetness_baseline: Mapping[int, float] = field(
        default_factory=_static_wetness_baseline
    )
    rain_saturation_scale_mm: Mapping[str, float] = field(
        default_factory=_rain_saturation_scales
    )
    rain_window_weights: Mapping[str, Mapping[str, float]] = field(
        default_factory=_rain_window_weights
    )
    rain_group_weights: Mapping[str, float] = field(default_factory=_rain_group_weights)
    temperature_window_weights: Mapping[str, float] = field(
        default_factory=_temperature_window_weights
    )
    relative_humidity_window_weights: Mapping[str, float] = field(
        default_factory=_relative_humidity_window_weights
    )
    drying_driver_weights: Mapping[str, float] = field(
        default_factory=_drying_driver_weights
    )
    temperature_no_drying_at_c: float = 5.0
    temperature_full_drying_at_c: float = 25.0
    relative_humidity_full_drying_at_percent: float = 40.0
    relative_humidity_no_drying_at_percent: float = 90.0
    rain_drying_relief: float = 0.65
    soil_retention_by_group: Mapping[str, float] = field(default_factory=_soil_retention)
    excluded_soil_groups: frozenset[str] = frozenset({"water", "ice"})
    terrain_full_retention_slope_degrees: float = 0.0
    terrain_no_retention_slope_degrees: float = 30.0
    neutral_retention: float = 0.50
    rain_recharge_effect: float = 0.35
    drying_pressure_effect: float = 0.30
    soil_retention_effect: float = 0.16
    terrain_retention_effect: float = 0.06
    input_completeness_weights: Mapping[str, float] = field(
        default_factory=_input_completeness_weights
    )

    def __post_init__(self) -> None:
        if set(self.static_wetness_baseline) != {1, 2, 3}:
            raise ValueError("static_wetness_baseline must define classes 1, 2 and 3 only")
        _validate_fractions("static_wetness_baseline", self.static_wetness_baseline.values())

        if set(self.rain_saturation_scale_mm) != set(RAIN_FIELDS):
            raise ValueError("rain_saturation_scale_mm must define every allowed rain window")
        if any(not math.isfinite(value) or value <= 0 for value in self.rain_saturation_scale_mm.values()):
            raise ValueError("rain saturation scales must be positive and finite")

        configured_rain_fields: list[str] = []
        for group, weights in self.rain_window_weights.items():
            _validate_weight_sum(f"rain_window_weights[{group}]", weights)
            configured_rain_fields.extend(weights)
        if sorted(configured_rain_fields) != sorted(RAIN_FIELDS):
            raise ValueError("rain_window_weights must use every rain window exactly once")
        if set(self.rain_window_weights) != set(self.rain_group_weights):
            raise ValueError("rain group names must match between window and group weights")
        _validate_weight_sum("rain_group_weights", self.rain_group_weights)
        _validate_weight_sum("temperature_window_weights", self.temperature_window_weights)
        if set(self.temperature_window_weights) != set(TEMPERATURE_FIELDS):
            raise ValueError("temperature_window_weights must define 3d, 7d and 14d")
        _validate_weight_sum(
            "relative_humidity_window_weights", self.relative_humidity_window_weights
        )
        if set(self.relative_humidity_window_weights) != set(RELATIVE_HUMIDITY_FIELDS):
            raise ValueError("relative_humidity_window_weights must define 3d and 7d")
        _validate_weight_sum("drying_driver_weights", self.drying_driver_weights)
        if set(self.drying_driver_weights) != {"temperature", "relative_humidity"}:
            raise ValueError("drying_driver_weights has unsupported drivers")

        if self.temperature_no_drying_at_c >= self.temperature_full_drying_at_c:
            raise ValueError("temperature drying curve endpoints must be increasing")
        if (
            self.relative_humidity_full_drying_at_percent
            >= self.relative_humidity_no_drying_at_percent
        ):
            raise ValueError("relative-humidity drying curve endpoints must be increasing")
        _validate_fraction("rain_drying_relief", self.rain_drying_relief)
        _validate_fractions("soil_retention_by_group", self.soil_retention_by_group.values())
        _validate_fraction("neutral_retention", self.neutral_retention)
        if self.terrain_full_retention_slope_degrees >= self.terrain_no_retention_slope_degrees:
            raise ValueError("terrain retention slope endpoints must be increasing")
        for name in (
            "rain_recharge_effect",
            "drying_pressure_effect",
            "soil_retention_effect",
            "terrain_retention_effect",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be non-negative and finite")
        _validate_weight_sum("input_completeness_weights", self.input_completeness_weights)
        expected_groups = {
            "static_wetness",
            "rain_history",
            "temperature",
            "relative_humidity",
            "soil",
            "slope",
        }
        if set(self.input_completeness_weights) != expected_groups:
            raise ValueError("input_completeness_weights has unsupported groups")


def _validate_fraction(name: str, value: float) -> None:
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} values must be finite and between 0 and 1")


def _validate_fractions(name: str, values: Sequence[float] | object) -> None:
    for value in values:  # type: ignore[union-attr]
        _validate_fraction(name, value)


def _validate_weight_sum(name: str, weights: Mapping[str, float]) -> None:
    _validate_fractions(name, weights.values())
    if not math.isclose(sum(weights.values()), 1.0, abs_tol=1e-9):
        raise ValueError(f"{name} must sum to 1")


def _clamp(value: float) -> float:
    return min(max(value, 0.0), 1.0)


def _linear_increasing(value: float, low: float, high: float) -> float:
    return _clamp((value - low) / (high - low))


def _linear_decreasing(value: float, low: float, high: float) -> float:
    return 1.0 - _linear_increasing(value, low, high)


def _saturating_rain_response(rainfall_mm: float, scale_mm: float) -> float:
    return 1.0 - math.exp(-rainfall_mm / scale_mm)


def _is_validated(provenance: FeatureProvenance | None) -> bool:
    if provenance is None or provenance.is_mock or provenance.is_nodata:
        return False
    status = provenance.semantic_status.lower()
    return "validated" in status and "unvalidated" not in status


def _validated_group(
    snapshot: FeatureSnapshot[object], names: Sequence[str]
) -> tuple[bool, tuple[str, ...]]:
    missing = tuple(
        name
        for name in names
        if getattr(snapshot.features, name) is None
        or not _is_validated(snapshot.feature_provenance.get(name))
    )
    return not missing, missing


def _group_quality(snapshot: FeatureSnapshot[object], names: Sequence[str]) -> float:
    return min(snapshot.feature_provenance[name].quality for name in names)


def _group_source(snapshot: FeatureSnapshot[object], names: Sequence[str]) -> str:
    return ",".join(sorted({snapshot.feature_provenance[name].source_name for name in names}))


def _weighted_feature_mean(features: object, weights: Mapping[str, float]) -> float:
    return sum(float(getattr(features, name)) * weight for name, weight in weights.items())


def _validated_exclusion_sources(
    habitat: FeatureSnapshot[StaticHabitatFeatures], config: CurrentSoilMoistureConfig
) -> tuple[str, ...]:
    sources = {
        item.source_name
        for item in habitat.feature_provenance.values()
        if _is_validated(item) and "habitat_exclusion_code" in item.details
    }
    wetness_provenance = habitat.feature_provenance.get("static_wetness_class")
    if habitat.features.static_wetness_class == 4 and _is_validated(wetness_provenance):
        sources.add(wetness_provenance.source_name)
    soil_provenance = habitat.feature_provenance.get("soil_group")
    if (
        habitat.features.soil_group in config.excluded_soil_groups
        and _is_validated(soil_provenance)
    ):
        sources.add(soil_provenance.source_name)
    return tuple(sorted(sources))


class CurrentSoilMoistureEstimator:
    """Estimate a 0..1 heuristic index from validated habitat and MESAN inputs."""

    def __init__(self, config: CurrentSoilMoistureConfig | None = None) -> None:
        self.config = config or CurrentSoilMoistureConfig()

    def estimate(
        self,
        habitat: FeatureSnapshot[StaticHabitatFeatures],
        weather: FeatureSnapshot[DynamicWeatherFeatures],
    ) -> CurrentSoilMoistureResult:
        config = self.config
        exclusion_sources = _validated_exclusion_sources(habitat, config)
        if exclusion_sources:
            return CurrentSoilMoistureResult(
                baseline_wetness=None,
                rain_recharge=None,
                drying_pressure=None,
                soil_retention=None,
                terrain_retention=None,
                estimated_current_soil_moisture=None,
                confidence=0.0,
                completeness=0.0,
                status="excluded_habitat",
                input_provenance={"habitat_exclusion": ",".join(exclusion_sources)},
            )

        static_ok, static_missing = _validated_group(habitat, ("static_wetness_class",))
        wetness_class = habitat.features.static_wetness_class
        static_ok = static_ok and wetness_class in config.static_wetness_baseline
        if not static_ok and "static_wetness_class" not in static_missing:
            static_missing = ("static_wetness_class",)

        rain_ok, rain_missing = _validated_group(weather, RAIN_FIELDS)
        temperature_ok, temperature_missing = _validated_group(weather, TEMPERATURE_FIELDS)
        humidity_ok, humidity_missing = _validated_group(weather, RELATIVE_HUMIDITY_FIELDS)

        soil_ok, soil_missing = _validated_group(habitat, ("soil_group",))
        soil_group = habitat.features.soil_group
        soil_ok = soil_ok and soil_group in config.soil_retention_by_group
        if not soil_ok and "soil_group" not in soil_missing:
            soil_missing = ("soil_group",)

        slope_ok, slope_missing = _validated_group(habitat, ("slope_degrees",))

        baseline = config.static_wetness_baseline[wetness_class] if static_ok else None
        rain_recharge = self._rain_recharge(weather.features) if rain_ok else None
        drying_pressure = (
            self._drying_pressure(weather.features, rain_recharge)
            if rain_ok and temperature_ok and humidity_ok and rain_recharge is not None
            else None
        )
        soil_retention = config.soil_retention_by_group[soil_group] if soil_ok else None
        terrain_retention = (
            self._terrain_retention(float(habitat.features.slope_degrees))
            if slope_ok
            else None
        )

        usage_by_group = {
            "static_wetness": static_ok,
            "rain_history": rain_ok,
            "temperature": temperature_ok,
            "relative_humidity": humidity_ok,
            "soil": soil_ok,
            "slope": slope_ok,
        }
        group_inputs = {
            "static_wetness": (habitat, ("static_wetness_class",)),
            "rain_history": (weather, RAIN_FIELDS),
            "temperature": (weather, TEMPERATURE_FIELDS),
            "relative_humidity": (weather, RELATIVE_HUMIDITY_FIELDS),
            "soil": (habitat, ("soil_group",)),
            "slope": (habitat, ("slope_degrees",)),
        }
        completeness = sum(
            config.input_completeness_weights[name]
            for name, used in usage_by_group.items()
            if used
        )
        confidence = sum(
            config.input_completeness_weights[name] * _group_quality(*group_inputs[name])
            for name, used in usage_by_group.items()
            if used
        )
        input_provenance = {
            name: _group_source(*group_inputs[name])
            for name, used in usage_by_group.items()
            if used
        }

        central_inputs_available = static_ok and rain_ok and temperature_ok and humidity_ok
        if central_inputs_available:
            assert baseline is not None
            assert rain_recharge is not None
            assert drying_pressure is not None
            soil_for_formula = (
                soil_retention if soil_retention is not None else config.neutral_retention
            )
            terrain_for_formula = (
                terrain_retention if terrain_retention is not None else config.neutral_retention
            )
            raw_estimate = (
                baseline
                + config.rain_recharge_effect * rain_recharge
                - config.drying_pressure_effect * drying_pressure
                + config.soil_retention_effect
                * (soil_for_formula - config.neutral_retention)
                + config.terrain_retention_effect
                * (terrain_for_formula - config.neutral_retention)
            )
            estimate = _clamp(raw_estimate)
            status = (
                "estimated_complete"
                if soil_ok and slope_ok
                else "estimated_optional_inputs_missing"
            )
        else:
            estimate = None
            status = "insufficient_central_inputs"

        missing_inputs = tuple(
            sorted(
                set(
                    static_missing
                    + rain_missing
                    + temperature_missing
                    + humidity_missing
                    + soil_missing
                    + slope_missing
                )
            )
        )
        return CurrentSoilMoistureResult(
            baseline_wetness=_rounded(baseline),
            rain_recharge=_rounded(rain_recharge),
            drying_pressure=_rounded(drying_pressure),
            soil_retention=_rounded(soil_retention),
            terrain_retention=_rounded(terrain_retention),
            estimated_current_soil_moisture=_rounded(estimate),
            confidence=round(confidence, 6),
            completeness=round(completeness, 6),
            status=status,
            used_inputs=MoistureInputUsage(
                used_static_wetness=static_ok,
                used_rain_history=rain_ok,
                used_temperature=temperature_ok,
                used_relative_humidity=humidity_ok,
                used_soil=soil_ok,
                used_slope=slope_ok,
            ),
            missing_inputs=missing_inputs,
            input_provenance=input_provenance,
        )

    def _rain_recharge(self, weather: DynamicWeatherFeatures) -> float:
        config = self.config
        group_scores = {
            group: sum(
                window_weight
                * _saturating_rain_response(
                    float(getattr(weather, feature)),
                    config.rain_saturation_scale_mm[feature],
                )
                for feature, window_weight in window_weights.items()
            )
            for group, window_weights in config.rain_window_weights.items()
        }
        return sum(
            config.rain_group_weights[group] * score
            for group, score in group_scores.items()
        )

    def _drying_pressure(
        self, weather: DynamicWeatherFeatures, rain_recharge: float
    ) -> float:
        config = self.config
        temperature = _weighted_feature_mean(weather, config.temperature_window_weights)
        humidity = _weighted_feature_mean(
            weather, config.relative_humidity_window_weights
        )
        temperature_drying = _linear_increasing(
            temperature,
            config.temperature_no_drying_at_c,
            config.temperature_full_drying_at_c,
        )
        humidity_drying = _linear_decreasing(
            humidity,
            config.relative_humidity_full_drying_at_percent,
            config.relative_humidity_no_drying_at_percent,
        )
        weather_drying = (
            config.drying_driver_weights["temperature"] * temperature_drying
            + config.drying_driver_weights["relative_humidity"] * humidity_drying
        )
        return _clamp(weather_drying * (1.0 - config.rain_drying_relief * rain_recharge))

    def _terrain_retention(self, slope_degrees: float) -> float:
        config = self.config
        return _linear_decreasing(
            slope_degrees,
            config.terrain_full_retention_slope_degrees,
            config.terrain_no_retention_slope_degrees,
        )


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 6)
