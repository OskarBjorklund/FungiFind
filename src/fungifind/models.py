"""Typed domain models shared by data sources, scorers, and future API layers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import date
from enum import Enum
from typing import Any, Generic, TypeVar


class Species(str, Enum):
    """Species supported by the first prototype."""

    CANTHARELLUS_CIBARIUS = "cantharellus_cibarius"
    CRATERELLUS_TUBAEFORMIS = "craterellus_tubaeformis"

    @classmethod
    def parse(cls, value: Species | str) -> Species:
        if isinstance(value, cls):
            return value
        try:
            return cls(value.strip().lower())
        except (AttributeError, ValueError) as exc:
            allowed = ", ".join(item.value for item in cls)
            raise ValueError(f"Unsupported species {value!r}. Expected one of: {allowed}") from exc


class SoilType(str, Enum):
    """Coarse v0 soil categories; adapters may map detailed source classes to these."""

    TILL = "till"
    SANDY = "sandy"
    CLAY = "clay"
    PEAT = "peat"
    ORGANIC = "organic"
    BEDROCK = "bedrock"
    UNKNOWN = "unknown"


def _validate_fraction(name: str, value: float | None) -> None:
    if value is not None and not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be between 0 and 1, got {value}")


def _validate_non_negative(name: str, value: float | None) -> None:
    if value is not None and value < 0:
        raise ValueError(f"{name} must be non-negative, got {value}")


@dataclass(frozen=True, slots=True)
class Location:
    """A public/API input location in WGS84 decimal degrees."""

    latitude: float = field(metadata={"unit": "degrees_north", "crs": "EPSG:4326"})
    longitude: float = field(metadata={"unit": "degrees_east", "crs": "EPSG:4326"})

    def __post_init__(self) -> None:
        if not -90.0 <= self.latitude <= 90.0:
            raise ValueError(f"latitude must be between -90 and 90, got {self.latitude}")
        if not -180.0 <= self.longitude <= 180.0:
            raise ValueError(f"longitude must be between -180 and 180, got {self.longitude}")


@dataclass(frozen=True, slots=True)
class StaticHabitatFeatures:
    """Long-lived site features. None always means explicitly unavailable."""

    forest_cover_fraction: float | None = field(default=None, metadata={"unit": "fraction"})
    canopy_density_fraction: float | None = field(default=None, metadata={"unit": "fraction"})
    spruce_fraction: float | None = field(default=None, metadata={"unit": "fraction"})
    pine_fraction: float | None = field(default=None, metadata={"unit": "fraction"})
    birch_fraction: float | None = field(default=None, metadata={"unit": "fraction"})
    other_deciduous_fraction: float | None = field(default=None, metadata={"unit": "fraction"})
    beech_fraction: float | None = field(default=None, metadata={"unit": "fraction"})
    oak_fraction: float | None = field(default=None, metadata={"unit": "fraction"})
    other_tree_fraction: float | None = field(default=None, metadata={"unit": "fraction"})
    soil_moisture_index: float | None = field(default=None, metadata={"unit": "index_0_1"})
    topographic_moisture_index: float | None = field(
        default=None, metadata={"unit": "normalized_index_0_1"}
    )
    elevation_m: float | None = field(default=None, metadata={"unit": "metres_above_sea_level"})
    slope_degrees: float | None = field(default=None, metadata={"unit": "degrees"})
    aspect_degrees: float | None = field(default=None, metadata={"unit": "degrees_from_north"})
    distance_to_water_m: float | None = field(default=None, metadata={"unit": "metres"})
    tree_height_m: float | None = field(default=None, metadata={"unit": "metres"})
    forest_mean_height: float | None = field(
        default=None, metadata={"unit": "source_unit_unvalidated"}
    )
    vegetation_ratio: float | None = field(
        default=None, metadata={"unit": "source_unit_unvalidated"}
    )
    basal_area: float | None = field(
        default=None, metadata={"unit": "source_unit_unvalidated"}
    )
    static_wetness_class: int | None = field(
        default=None,
        metadata={
            "unit": "category",
            "temporal_meaning": "long_term_static_hydrological_wetness_potential",
        },
    )
    static_wetness_label: str | None = field(
        default=None,
        metadata={
            "unit": "category_label",
            "temporal_meaning": "long_term_static_hydrological_wetness_potential",
        },
    )
    soil_type: SoilType | None = field(default=None, metadata={"unit": "category"})

    def __post_init__(self) -> None:
        for name in (
            "forest_cover_fraction",
            "canopy_density_fraction",
            "spruce_fraction",
            "pine_fraction",
            "birch_fraction",
            "other_deciduous_fraction",
            "beech_fraction",
            "oak_fraction",
            "other_tree_fraction",
            "soil_moisture_index",
            "topographic_moisture_index",
        ):
            _validate_fraction(name, getattr(self, name))
        for name in ("elevation_m", "slope_degrees", "distance_to_water_m", "tree_height_m"):
            _validate_non_negative(name, getattr(self, name))
        if self.slope_degrees is not None and self.slope_degrees > 90:
            raise ValueError("slope_degrees cannot exceed 90")
        if self.aspect_degrees is not None and not 0 <= self.aspect_degrees <= 360:
            raise ValueError("aspect_degrees must be between 0 and 360")
        if self.static_wetness_class is not None:
            if isinstance(self.static_wetness_class, bool) or not isinstance(
                self.static_wetness_class, int
            ):
                raise ValueError("static_wetness_class must be an integer class")
            if self.static_wetness_class < 0:
                raise ValueError("static_wetness_class must be non-negative")


@dataclass(frozen=True, slots=True)
class DynamicWeatherFeatures:
    """Date-dependent weather features, with cumulative rainfall ending at the query date."""

    rainfall_1d_mm: float | None = field(default=None, metadata={"unit": "millimetres"})
    rainfall_3d_mm: float | None = field(default=None, metadata={"unit": "millimetres"})
    rainfall_7d_mm: float | None = field(default=None, metadata={"unit": "millimetres"})
    rainfall_14d_mm: float | None = field(default=None, metadata={"unit": "millimetres"})
    rainfall_21d_mm: float | None = field(default=None, metadata={"unit": "millimetres"})
    rainfall_30d_mm: float | None = field(default=None, metadata={"unit": "millimetres"})
    mean_temperature_c: float | None = field(default=None, metadata={"unit": "degrees_celsius"})
    min_temperature_c: float | None = field(default=None, metadata={"unit": "degrees_celsius"})
    max_temperature_c: float | None = field(default=None, metadata={"unit": "degrees_celsius"})
    estimated_current_soil_moisture_index: float | None = field(
        default=None,
        metadata={"unit": "index_0_1", "temporal_meaning": "date_dependent_current_estimate"},
    )
    evapotranspiration_7d_mm: float | None = field(
        default=None, metadata={"unit": "millimetres"}
    )
    dry_days_count_14d: int | None = field(default=None, metadata={"unit": "days"})
    temperature_trend_c_per_day: float | None = field(
        default=None, metadata={"unit": "degrees_celsius_per_day"}
    )

    def __post_init__(self) -> None:
        for name in (
            "rainfall_1d_mm",
            "rainfall_3d_mm",
            "rainfall_7d_mm",
            "rainfall_14d_mm",
            "rainfall_21d_mm",
            "rainfall_30d_mm",
            "evapotranspiration_7d_mm",
            "dry_days_count_14d",
        ):
            _validate_non_negative(name, getattr(self, name))
        _validate_fraction(
            "estimated_current_soil_moisture_index",
            self.estimated_current_soil_moisture_index,
        )
        if self.dry_days_count_14d is not None and self.dry_days_count_14d > 14:
            raise ValueError("dry_days_count_14d cannot exceed 14")


@dataclass(frozen=True, slots=True)
class DataSourceMetadata:
    """Provenance and coarse source quality used by the confidence indicator."""

    source_name: str
    quality: float
    is_mock: bool = False
    details: Mapping[str, str | float | int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_fraction("quality", self.quality)


@dataclass(frozen=True, slots=True)
class FeatureProvenance:
    """Per-feature origin retained through hybrid sources and model results."""

    source_name: str
    quality: float
    is_mock: bool
    semantic_status: str
    source_path: str | None = None
    raw_value: float | int | None = None
    interpreted_value: float | int | None = None
    is_nodata: bool = False
    grid_signature: str | None = None
    details: Mapping[str, str | float | int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_fraction("quality", self.quality)


FeatureT = TypeVar("FeatureT")


@dataclass(frozen=True, slots=True)
class FeatureSnapshot(Generic[FeatureT]):
    features: FeatureT
    metadata: DataSourceMetadata
    feature_provenance: Mapping[str, FeatureProvenance] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ModelResult:
    """A heuristic suitability result. It is deliberately not named probability."""

    species: Species
    location: Location
    date: date
    habitat_score: float | None
    fruiting_score: float | None
    final_score: float
    confidence: float
    factors: Mapping[str, float]
    missing_features: tuple[str, ...]
    data_sources: Mapping[str, str]
    feature_provenance: Mapping[str, FeatureProvenance] = field(default_factory=dict)
    score_type: str = "heuristic_suitability_index_v0"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["species"] = self.species.value
        payload["date"] = self.date.isoformat()
        return payload
