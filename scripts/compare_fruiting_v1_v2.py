"""Compare production fruiting v1 with the parallel v2 experiment on real points."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from fungifind.data_sources import (
    CompositeHabitatDataSource,
    ElevationTileNotFoundError,
    ForestProfileRasterDataSource,
    ForestShareInterpretation,
    MesanWeatherHistoryDataSource,
    NmdLandcoverRasterDataSource,
    SguSoilVectorDataSource,
    StaticWetnessRasterDataSource,
    TerrainDemReader,
)
from fungifind.fruiting_v2 import ExperimentalFruitingV2Engine
from fungifind.models import (
    DataSourceMetadata,
    FeatureSnapshot,
    Location,
    Species,
    StaticHabitatFeatures,
)
from fungifind.moisture import CurrentSoilMoistureEstimator
from fungifind.scoring import RuleBasedScoringEngine

DEFAULT_TARGET_DATE = date(2026, 8, 27)
DEFAULT_REPORT = Path("reports/fruiting_v1_v2_real_points.json")

# All points are searchable according to the local validated NMD layer and are
# within 5 km of one of the two fully backfilled MESAN grid points.
REAL_TEST_POINTS = {
    "class1_coarse": Location(59.166136, 18.242348),
    "class2_moraine": Location(59.160136, 18.247348),
    "class3_fine": Location(59.163136, 18.247348),
    "class2_peat": Location(59.139136, 18.237348),
    "class1_peat": Location(59.139136, 18.242348),
    "class3_peat": Location(59.166136, 18.282348),
    "flat_class2_fine": Location(59.412897, 18.084709),
    "sloping_class1_bedrock": Location(59.412897, 18.154709),
    "steep_class1_moraine": Location(59.412897, 18.169709),
    "sloping_class2_coarse": Location(59.436897, 18.109709),
}


class _OptionalTerrainDataSource:
    def __init__(self, manifest: Path) -> None:
        self.reader = TerrainDemReader.from_manifest(manifest)

    def get_features(self, location: Location) -> FeatureSnapshot[StaticHabitatFeatures]:
        try:
            return self.reader.sample_terrain(location).snapshot
        except ElevationTileNotFoundError:
            return FeatureSnapshot(
                features=StaticHabitatFeatures(),
                metadata=DataSourceMetadata(
                    source_name="lantmateriet_dtm_not_downloaded_at_location",
                    quality=0.0,
                    details={"optional_slope_status": "no_local_dem_tile"},
                ),
            )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare production fruiting v1 and experimental fruiting v2"
    )
    parser.add_argument("--date", type=date.fromisoformat, default=DEFAULT_TARGET_DATE)
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("src/data/weather/mesan_history.sqlite"),
    )
    parser.add_argument(
        "--elevation-manifest",
        type=Path,
        default=Path("src/data/elevation/manifest.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        help=f"Optional JSON report path; recommended: {DEFAULT_REPORT}",
    )
    return parser


def _habitat_source(elevation_manifest: Path) -> CompositeHabitatDataSource:
    return CompositeHabitatDataSource(
        {
            "tree_species": ForestProfileRasterDataSource.from_kind_directory(
                interpretation=(
                    ForestShareInterpretation.PROVISIONAL_ZERO_TO_HUNDRED_SHARE
                )
            ),
            "static_wetness": StaticWetnessRasterDataSource.slu_classified(
                "src/data/misc_data/SLUMarkfuktighetKlassad.tif"
            ),
            "landcover": NmdLandcoverRasterDataSource.nmd2023_v2_1(),
            "soil": SguSoilVectorDataSource.official(),
            "terrain": _OptionalTerrainDataSource(elevation_manifest),
        }
    )


def _species_report(
    *,
    species: Species,
    target_date: date,
    location: Location,
    habitat: FeatureSnapshot[StaticHabitatFeatures],
    weather,
    moisture,
    scoring_engine: RuleBasedScoringEngine,
    fruiting_v2_engine: ExperimentalFruitingV2Engine,
) -> dict[str, object]:
    production = scoring_engine.score(species, location, target_date, habitat, weather)
    v2 = fruiting_v2_engine.evaluate(
        species=species,
        target_date=target_date,
        weather=weather.features,
        moisture=moisture,
        production_result=production,
    )
    if v2.status != "available":
        raise RuntimeError(f"{species.value} v2 is {v2.status} at {location}")
    assert production.fruiting_score is not None
    assert production.final_score is not None
    assert v2.fruiting_score_v2 is not None
    assert v2.final_score_v2 is not None
    return {
        "species": species.value,
        "habitat_score": production.habitat_score,
        "old": {
            "precipitation": production.factors.get("precipitation"),
            "temperature": production.factors.get("temperature"),
            "relative_humidity": production.factors.get("relative_humidity"),
            "season": production.factors.get("season"),
            "fruiting_score": production.fruiting_score,
            "final_score": production.final_score,
        },
        "v2": {
            "estimated_current_soil_moisture": v2.current_soil_moisture,
            "moisture_preference": v2.moisture_preference_score,
            "recent_rain_trigger": v2.recent_rain_trigger,
            "temperature": v2.temperature_suitability,
            "season": v2.season_suitability,
            "fruiting_score_v2": v2.fruiting_score_v2,
            "final_score_v2": v2.final_score_v2,
            "moisture_confidence": v2.moisture_confidence,
            "moisture_completeness": v2.moisture_completeness,
            "status": v2.status,
        },
        "delta": {
            "fruiting_v2_minus_v1": round(
                v2.fruiting_score_v2 - production.fruiting_score, 6
            ),
            "final_v2_minus_v1": round(v2.final_score_v2 - production.final_score, 6),
        },
    }


def _point_report(
    *,
    label: str,
    location: Location,
    target_date: date,
    habitat_source: CompositeHabitatDataSource,
    weather_source: MesanWeatherHistoryDataSource,
    moisture_estimator: CurrentSoilMoistureEstimator,
    scoring_engine: RuleBasedScoringEngine,
    fruiting_v2_engine: ExperimentalFruitingV2Engine,
) -> dict[str, object]:
    habitat = habitat_source.get_features(location)
    weather = weather_source.get_features(location, target_date)
    moisture = moisture_estimator.estimate(habitat, weather)
    if moisture.estimated_current_soil_moisture is None:
        raise RuntimeError(f"{label} moisture is {moisture.status}")
    weather_details = weather.metadata.details
    return {
        "label": label,
        "latitude": location.latitude,
        "longitude": location.longitude,
        "target_date": target_date.isoformat(),
        "mesan_grid": {
            "latitude": weather_details["grid_latitude"],
            "longitude": weather_details["grid_longitude"],
            "distance_m": weather_details["grid_distance_m"],
        },
        "habitat": {
            "static_wetness_class": habitat.features.static_wetness_class,
            "soil_group": habitat.features.soil_group,
            "slope_degrees": habitat.features.slope_degrees,
            "landcover_label": habitat.features.landcover_label,
        },
        "species_results": [
            _species_report(
                species=species,
                target_date=target_date,
                location=location,
                habitat=habitat,
                weather=weather,
                moisture=moisture,
                scoring_engine=scoring_engine,
                fruiting_v2_engine=fruiting_v2_engine,
            )
            for species in Species
        ],
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = _parser().parse_args()
    try:
        habitat_source = _habitat_source(args.elevation_manifest)
        weather_source = MesanWeatherHistoryDataSource(args.database)
        moisture_estimator = CurrentSoilMoistureEstimator()
        scoring_engine = RuleBasedScoringEngine()
        fruiting_v2_engine = ExperimentalFruitingV2Engine()
        payload = {
            "experiment_version": "fruiting_scoring_v2_experiment_v1",
            "production_scores_unchanged": True,
            "target_date": args.date.isoformat(),
            "point_count": len(REAL_TEST_POINTS),
            "species_count": len(Species),
            "points": [
                _point_report(
                    label=label,
                    location=location,
                    target_date=args.date,
                    habitat_source=habitat_source,
                    weather_source=weather_source,
                    moisture_estimator=moisture_estimator,
                    scoring_engine=scoring_engine,
                    fruiting_v2_engine=fruiting_v2_engine,
                )
                for label, location in REAL_TEST_POINTS.items()
            ],
        }
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
