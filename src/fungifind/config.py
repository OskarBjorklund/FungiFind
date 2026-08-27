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
    rainfall_group_windows: Mapping[str, Mapping[str, float]]
    rainfall_preferences: Mapping[str, TrapezoidPreference]
    temperature_weights: Mapping[str, float]
    temperature_preferences: Mapping[str, TrapezoidPreference]
    relative_humidity_weights: Mapping[str, float]
    relative_humidity_preferences: Mapping[str, TrapezoidPreference]
    season_preference: TrapezoidPreference
    fruiting_v2_component_weights: Mapping[str, float]
    current_soil_moisture_preference: TrapezoidPreference
    dry_spell_scoring_enabled: bool


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
SPECIES_CONFIGS: Mapping[Species, SpeciesConfig] = {
    Species.CANTHARELLUS_CIBARIUS: SpeciesConfig(
        species=Species.CANTHARELLUS_CIBARIUS,
        common_name_sv="kantarell",
        habitat_component_weights=_COMMON_HABITAT_WEIGHTS,
        fruiting_component_weights={
            "recent_rain": 0.10,
            "medium_term_rain": 0.22,
            "background_rain": 0.13,
            "temperature": 0.30,
            "relative_humidity": 0.10,
            "season": 0.15,
        },
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
        rainfall_group_windows={
            "recent_rain": {"rainfall_1d_mm": 0.35, "rainfall_3d_mm": 0.65},
            "medium_term_rain": {"rainfall_7d_mm": 0.45, "rainfall_14d_mm": 0.55},
            "background_rain": {"rainfall_21d_mm": 0.45, "rainfall_30d_mm": 0.55},
        },
        rainfall_preferences={
            "rainfall_1d_mm": TrapezoidPreference(0.0, 2.0, 10.0, 30.0),
            "rainfall_3d_mm": TrapezoidPreference(0.0, 4.0, 20.0, 55.0),
            "rainfall_7d_mm": TrapezoidPreference(2.0, 14.0, 42.0, 90.0),
            "rainfall_14d_mm": TrapezoidPreference(8.0, 30.0, 75.0, 150.0),
            "rainfall_21d_mm": TrapezoidPreference(12.0, 42.0, 105.0, 210.0),
            "rainfall_30d_mm": TrapezoidPreference(18.0, 55.0, 145.0, 280.0),
        },
        temperature_weights={
            "temp_mean_3d_c": 0.20,
            "temp_mean_7d_c": 0.50,
            "temp_mean_14d_c": 0.30,
        },
        temperature_preferences={
            "temp_mean_3d_c": TrapezoidPreference(4.0, 10.0, 18.0, 25.0),
            "temp_mean_7d_c": TrapezoidPreference(4.0, 9.0, 17.0, 24.0),
            "temp_mean_14d_c": TrapezoidPreference(3.0, 8.0, 16.0, 23.0),
        },
        relative_humidity_weights={
            "relative_humidity_mean_3d_percent": 0.40,
            "relative_humidity_mean_7d_percent": 0.60,
        },
        relative_humidity_preferences={
            "relative_humidity_mean_3d_percent": TrapezoidPreference(
                35.0, 70.0, 100.0, 100.0
            ),
            "relative_humidity_mean_7d_percent": TrapezoidPreference(
                35.0, 68.0, 100.0, 100.0
            ),
        },
        season_preference=TrapezoidPreference(175, 215, 275, 315),
        fruiting_v2_component_weights={
            "current_soil_moisture": 0.45,
            "temperature": 0.25,
            "season": 0.20,
            "recent_rain_trigger": 0.10,
        },
        # Preliminary biological assumption: moderate-to-fairly-moist soil is
        # preferred; near-saturation is deliberately outside the optimum.
        current_soil_moisture_preference=TrapezoidPreference(0.18, 0.45, 0.72, 0.92),
        dry_spell_scoring_enabled=False,
    ),
    Species.CRATERELLUS_TUBAEFORMIS: SpeciesConfig(
        species=Species.CRATERELLUS_TUBAEFORMIS,
        common_name_sv="trattkantarell",
        habitat_component_weights=_COMMON_HABITAT_WEIGHTS,
        fruiting_component_weights={
            "recent_rain": 0.08,
            "medium_term_rain": 0.20,
            "background_rain": 0.17,
            "temperature": 0.28,
            "relative_humidity": 0.12,
            "season": 0.15,
        },
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
        rainfall_group_windows={
            "recent_rain": {"rainfall_1d_mm": 0.30, "rainfall_3d_mm": 0.70},
            "medium_term_rain": {"rainfall_7d_mm": 0.40, "rainfall_14d_mm": 0.60},
            "background_rain": {"rainfall_21d_mm": 0.40, "rainfall_30d_mm": 0.60},
        },
        rainfall_preferences={
            "rainfall_1d_mm": TrapezoidPreference(0.0, 1.5, 10.0, 30.0),
            "rainfall_3d_mm": TrapezoidPreference(0.0, 3.0, 22.0, 60.0),
            "rainfall_7d_mm": TrapezoidPreference(3.0, 16.0, 48.0, 100.0),
            "rainfall_14d_mm": TrapezoidPreference(10.0, 36.0, 90.0, 175.0),
            "rainfall_21d_mm": TrapezoidPreference(16.0, 52.0, 130.0, 240.0),
            "rainfall_30d_mm": TrapezoidPreference(22.0, 70.0, 175.0, 320.0),
        },
        temperature_weights={
            "temp_mean_3d_c": 0.15,
            "temp_mean_7d_c": 0.45,
            "temp_mean_14d_c": 0.40,
        },
        temperature_preferences={
            "temp_mean_3d_c": TrapezoidPreference(-1.0, 5.0, 14.0, 21.0),
            "temp_mean_7d_c": TrapezoidPreference(-1.0, 5.0, 13.5, 20.0),
            "temp_mean_14d_c": TrapezoidPreference(-2.0, 4.0, 13.0, 19.0),
        },
        relative_humidity_weights={
            "relative_humidity_mean_3d_percent": 0.35,
            "relative_humidity_mean_7d_percent": 0.65,
        },
        relative_humidity_preferences={
            "relative_humidity_mean_3d_percent": TrapezoidPreference(
                40.0, 75.0, 100.0, 100.0
            ),
            "relative_humidity_mean_7d_percent": TrapezoidPreference(
                40.0, 73.0, 100.0, 100.0
            ),
        },
        season_preference=TrapezoidPreference(225, 255, 305, 340),
        fruiting_v2_component_weights={
            "current_soil_moisture": 0.50,
            "temperature": 0.22,
            "season": 0.18,
            "recent_rain_trigger": 0.10,
        },
        # Preliminary biological assumption: the optimum is shifted toward
        # wetter soil than for C. cibarius, but not to saturated conditions.
        current_soil_moisture_preference=TrapezoidPreference(0.25, 0.55, 0.82, 0.97),
        dry_spell_scoring_enabled=False,
    ),
}


def get_species_config(species: Species | str) -> SpeciesConfig:
    return SPECIES_CONFIGS[Species.parse(species)]
