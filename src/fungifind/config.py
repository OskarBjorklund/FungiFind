"""Preliminary species parameters, deliberately separated from scoring code."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from fungifind.models import SoilType, Species


@dataclass(frozen=True, slots=True)
class TrapezoidPreference:
    """A transparent 0..1 preference curve with a flat optimal interval."""

    low_zero: float
    low_optimal: float
    high_optimal: float
    high_zero: float

    def __post_init__(self) -> None:
        if not self.low_zero <= self.low_optimal <= self.high_optimal <= self.high_zero:
            raise ValueError("Preference points must be monotonically increasing")

    def score(self, value: float) -> float:
        if value < self.low_zero or value > self.high_zero:
            return 0.0
        if self.low_optimal <= value <= self.high_optimal:
            return 1.0
        if value < self.low_optimal:
            width = self.low_optimal - self.low_zero
            return 1.0 if width == 0 else (value - self.low_zero) / width
        width = self.high_zero - self.high_optimal
        return 1.0 if width == 0 else (self.high_zero - value) / width


@dataclass(frozen=True, slots=True)
class SpeciesConfig:
    species: Species
    common_name_sv: str
    habitat_component_weights: Mapping[str, float]
    fruiting_component_weights: Mapping[str, float]
    final_habitat_weight: float
    forest_weights: Mapping[str, float]
    forest_preferences: Mapping[str, TrapezoidPreference]
    tree_affinities: Mapping[str, float]
    static_moisture_weights: Mapping[str, float]
    static_moisture_preferences: Mapping[str, TrapezoidPreference]
    static_wetness_preferences: Mapping[int, float]
    terrain_weights: Mapping[str, float]
    terrain_preferences: Mapping[str, TrapezoidPreference]
    soil_affinities: Mapping[SoilType, float]
    rainfall_weights: Mapping[str, float]
    rainfall_preferences: Mapping[str, TrapezoidPreference]
    recent_moisture_preference: TrapezoidPreference
    temperature_weights: Mapping[str, float]
    temperature_preferences: Mapping[str, TrapezoidPreference]
    season_preference: TrapezoidPreference
    drought_weights: Mapping[str, float]
    drought_preferences: Mapping[str, TrapezoidPreference]


# All values below are preliminary biological assumptions for a software prototype.
# They have not been fitted, calibrated, or validated against mushroom observations.
_COMMON_HABITAT_WEIGHTS = {
    # The prior five-component ratios are preserved inside 85% of the new
    # habitat score. If static wetness is unavailable, scoring drops this
    # optional component and exactly recovers the previous relative weights.
    "forest": 0.204,
    "tree_species": 0.204,
    "soil_moisture": 0.187,
    "terrain": 0.1275,
    "soil": 0.1275,
    "static_wetness": 0.15,
}
_COMMON_FRUITING_WEIGHTS = {
    "rain_history": 0.30,
    "recent_moisture": 0.24,
    "temperature": 0.20,
    "season": 0.16,
    "drought": 0.10,
}


SPECIES_CONFIGS: Mapping[Species, SpeciesConfig] = {
    Species.CANTHARELLUS_CIBARIUS: SpeciesConfig(
        species=Species.CANTHARELLUS_CIBARIUS,
        common_name_sv="kantarell",
        habitat_component_weights=_COMMON_HABITAT_WEIGHTS,
        fruiting_component_weights=_COMMON_FRUITING_WEIGHTS,
        final_habitat_weight=0.60,
        forest_weights={"forest_cover_fraction": 0.60, "canopy_density_fraction": 0.40},
        forest_preferences={
            "forest_cover_fraction": TrapezoidPreference(0.20, 0.60, 0.95, 1.00),
            "canopy_density_fraction": TrapezoidPreference(0.15, 0.45, 0.80, 1.00),
        },
        tree_affinities={
            "spruce_fraction": 0.95,
            "pine_fraction": 0.78,
            "birch_fraction": 0.88,
            "other_deciduous_fraction": 0.40,
            "beech_fraction": 0.70,
            "oak_fraction": 0.62,
            "other_tree_fraction": 0.40,
        },
        static_moisture_weights={"soil_moisture_index": 0.65, "topographic_moisture_index": 0.35},
        static_moisture_preferences={
            "soil_moisture_index": TrapezoidPreference(0.15, 0.45, 0.78, 0.98),
            "topographic_moisture_index": TrapezoidPreference(0.10, 0.38, 0.76, 0.98),
        },
        # Biologically preliminary preferences; class meanings are official,
        # but these mushroom suitability weights are not field-validated.
        static_wetness_preferences={1: 0.55, 2: 1.00, 3: 0.75, 4: 0.00},
        terrain_weights={"slope_degrees": 0.45, "elevation_m": 0.35, "distance_to_water_m": 0.20},
        terrain_preferences={
            "slope_degrees": TrapezoidPreference(0.0, 2.0, 18.0, 42.0),
            "elevation_m": TrapezoidPreference(0.0, 15.0, 420.0, 850.0),
            "distance_to_water_m": TrapezoidPreference(0.0, 50.0, 1200.0, 6000.0),
        },
        soil_affinities={
            SoilType.TILL: 0.95,
            SoilType.SANDY: 0.72,
            SoilType.CLAY: 0.42,
            SoilType.PEAT: 0.48,
            SoilType.ORGANIC: 0.70,
            SoilType.BEDROCK: 0.35,
            SoilType.UNKNOWN: 0.50,
        },
        rainfall_weights={
            "rainfall_3d_mm": 0.10,
            "rainfall_7d_mm": 0.25,
            "rainfall_14d_mm": 0.35,
            "rainfall_21d_mm": 0.20,
            "rainfall_30d_mm": 0.10,
        },
        rainfall_preferences={
            "rainfall_3d_mm": TrapezoidPreference(0.0, 4.0, 20.0, 55.0),
            "rainfall_7d_mm": TrapezoidPreference(2.0, 14.0, 42.0, 90.0),
            "rainfall_14d_mm": TrapezoidPreference(8.0, 30.0, 75.0, 150.0),
            "rainfall_21d_mm": TrapezoidPreference(12.0, 42.0, 105.0, 210.0),
            "rainfall_30d_mm": TrapezoidPreference(18.0, 55.0, 145.0, 280.0),
        },
        recent_moisture_preference=TrapezoidPreference(0.18, 0.48, 0.82, 0.98),
        temperature_weights={
            "mean_temperature_c": 0.60,
            "min_temperature_c": 0.20,
            "max_temperature_c": 0.20,
        },
        temperature_preferences={
            "mean_temperature_c": TrapezoidPreference(4.0, 10.0, 17.0, 24.0),
            "min_temperature_c": TrapezoidPreference(-1.0, 5.0, 13.0, 18.0),
            "max_temperature_c": TrapezoidPreference(9.0, 15.0, 23.0, 31.0),
        },
        season_preference=TrapezoidPreference(175, 215, 275, 315),
        drought_weights={"dry_days_count_14d": 0.70, "evapotranspiration_7d_mm": 0.30},
        drought_preferences={
            "dry_days_count_14d": TrapezoidPreference(0.0, 0.0, 4.0, 14.0),
            "evapotranspiration_7d_mm": TrapezoidPreference(0.0, 4.0, 20.0, 42.0),
        },
    ),
    Species.CRATERELLUS_TUBAEFORMIS: SpeciesConfig(
        species=Species.CRATERELLUS_TUBAEFORMIS,
        common_name_sv="trattkantarell",
        habitat_component_weights=_COMMON_HABITAT_WEIGHTS,
        fruiting_component_weights=_COMMON_FRUITING_WEIGHTS,
        final_habitat_weight=0.55,
        forest_weights={"forest_cover_fraction": 0.55, "canopy_density_fraction": 0.45},
        forest_preferences={
            "forest_cover_fraction": TrapezoidPreference(0.30, 0.68, 1.00, 1.00),
            "canopy_density_fraction": TrapezoidPreference(0.20, 0.55, 0.92, 1.00),
        },
        tree_affinities={
            "spruce_fraction": 1.00,
            "pine_fraction": 0.70,
            "birch_fraction": 0.55,
            "other_deciduous_fraction": 0.35,
            "beech_fraction": 0.52,
            "oak_fraction": 0.38,
            "other_tree_fraction": 0.35,
        },
        static_moisture_weights={"soil_moisture_index": 0.60, "topographic_moisture_index": 0.40},
        static_moisture_preferences={
            "soil_moisture_index": TrapezoidPreference(0.24, 0.58, 0.88, 1.00),
            "topographic_moisture_index": TrapezoidPreference(0.20, 0.52, 0.88, 1.00),
        },
        # Biologically preliminary preferences; class meanings are official,
        # but these mushroom suitability weights are not field-validated.
        static_wetness_preferences={1: 0.35, 2: 0.90, 3: 1.00, 4: 0.00},
        terrain_weights={"slope_degrees": 0.40, "elevation_m": 0.35, "distance_to_water_m": 0.25},
        terrain_preferences={
            "slope_degrees": TrapezoidPreference(0.0, 1.0, 16.0, 38.0),
            "elevation_m": TrapezoidPreference(0.0, 20.0, 550.0, 950.0),
            "distance_to_water_m": TrapezoidPreference(0.0, 30.0, 900.0, 4500.0),
        },
        soil_affinities={
            SoilType.TILL: 1.00,
            SoilType.SANDY: 0.58,
            SoilType.CLAY: 0.34,
            SoilType.PEAT: 0.78,
            SoilType.ORGANIC: 0.88,
            SoilType.BEDROCK: 0.28,
            SoilType.UNKNOWN: 0.50,
        },
        rainfall_weights={
            "rainfall_3d_mm": 0.05,
            "rainfall_7d_mm": 0.18,
            "rainfall_14d_mm": 0.32,
            "rainfall_21d_mm": 0.27,
            "rainfall_30d_mm": 0.18,
        },
        rainfall_preferences={
            "rainfall_3d_mm": TrapezoidPreference(0.0, 3.0, 22.0, 60.0),
            "rainfall_7d_mm": TrapezoidPreference(3.0, 16.0, 48.0, 100.0),
            "rainfall_14d_mm": TrapezoidPreference(10.0, 36.0, 90.0, 175.0),
            "rainfall_21d_mm": TrapezoidPreference(16.0, 52.0, 130.0, 240.0),
            "rainfall_30d_mm": TrapezoidPreference(22.0, 70.0, 175.0, 320.0),
        },
        recent_moisture_preference=TrapezoidPreference(0.25, 0.58, 0.90, 1.00),
        temperature_weights={
            "mean_temperature_c": 0.60,
            "min_temperature_c": 0.20,
            "max_temperature_c": 0.20,
        },
        temperature_preferences={
            "mean_temperature_c": TrapezoidPreference(0.0, 6.0, 13.5, 20.0),
            "min_temperature_c": TrapezoidPreference(-5.0, 1.0, 9.0, 15.0),
            "max_temperature_c": TrapezoidPreference(5.0, 10.0, 19.0, 27.0),
        },
        season_preference=TrapezoidPreference(225, 255, 305, 340),
        drought_weights={"dry_days_count_14d": 0.75, "evapotranspiration_7d_mm": 0.25},
        drought_preferences={
            "dry_days_count_14d": TrapezoidPreference(0.0, 0.0, 3.0, 12.0),
            "evapotranspiration_7d_mm": TrapezoidPreference(0.0, 3.0, 16.0, 35.0),
        },
    ),
}


def get_species_config(species: Species | str) -> SpeciesConfig:
    return SPECIES_CONFIGS[Species.parse(species)]
