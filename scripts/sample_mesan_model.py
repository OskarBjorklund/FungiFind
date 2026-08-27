"""Run the local habitat pipeline with coverage-aware MESAN history weather."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from fungifind.data_sources import (
    CompositeHabitatDataSource,
    ForestProfileRasterDataSource,
    ForestShareInterpretation,
    HybridHabitatDataSource,
    MesanWeatherHistoryDataSource,
    MockHabitatDataSource,
    MockWeatherDataSource,
    NmdLandcoverRasterDataSource,
    SguSoilVectorDataSource,
    StaticWetnessRasterDataSource,
)
from fungifind.models import Location, Species
from fungifind.service import MushroomScoringService


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sample real local habitat layers and MESAN history scoring"
    )
    parser.add_argument("--latitude", type=float, required=True)
    parser.add_argument("--longitude", type=float, required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument(
        "--species",
        action="append",
        choices=[species.value for species in Species],
        help="Repeat to select species; defaults to both supported species.",
    )
    parser.add_argument(
        "--database", type=Path, default=Path("src/data/weather/mesan_history.sqlite")
    )
    return parser


def _local_habitat_source() -> HybridHabitatDataSource:
    real = CompositeHabitatDataSource(
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
        }
    )
    return HybridHabitatDataSource(real, MockHabitatDataSource())


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = _parser().parse_args()
    species_values = args.species or [species.value for species in Species]
    location = Location(args.latitude, args.longitude)
    weather_source = MesanWeatherHistoryDataSource(args.database)
    try:
        target_date = date.fromisoformat(args.date)
        weather, resolution = weather_source.get_history_features(location, target_date)
        weather_snapshot = weather_source.get_features(location, target_date)
        habitat_source = _local_habitat_source()
        real_service = MushroomScoringService(habitat_source, weather_source)
        mock_service = MushroomScoringService(habitat_source, MockWeatherDataSource())
        results = [
            (
                species,
                real_service.get_score(
                    location.latitude, location.longitude, args.date, species
                ),
                mock_service.get_score(
                    location.latitude, location.longitude, args.date, species
                ),
            )
            for species in species_values
        ]
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Location: {location.latitude}, {location.longitude}")
    print(f"Target date: {args.date}")
    print(
        "MESAN grid: "
        f"{resolution.grid_point.latitude}, {resolution.grid_point.longitude} "
        f"({resolution.grid_distance_m:.1f} m from request)"
    )
    print(f"Aggregate end: {resolution.target_time.isoformat()}")
    print("\nWEATHER HISTORY")
    for name, aggregate in weather.aggregates():
        if name in {"days_since_significant_rain", "dry_spell_length"}:
            continue
        value = "None" if aggregate.value is None else f"{aggregate.value:.6f}"
        print(
            f"{name}: {value} {aggregate.unit}; {aggregate.status.value}; "
            f"{aggregate.available_hours}/{aggregate.expected_hours} h"
        )
    products = weather_snapshot.feature_provenance["rainfall_30d_mm"].details[
        "source_products"
    ]
    print(f"Source products: {products}")
    print("Dry-period scoring: disabled (no biological threshold configured)")

    for species, result, mock_result in results:
        print(f"\nMODEL: {species}")
        print(f"Eligibility: {result.eligibility_status}")
        if result.habitat_exclusions:
            reasons = ", ".join(item.reason_code for item in result.habitat_exclusions)
            print(f"Exclusion reasons: {reasons}")
        for factor_name in (
            "recent_rain",
            "medium_term_rain",
            "background_rain",
            "precipitation",
            "temperature",
            "relative_humidity",
        ):
            print(f"{factor_name}: {result.factors.get(factor_name)}")
        print(f"Fruiting score: {result.fruiting_score}")
        print(f"Habitat score: {result.habitat_score}")
        print(f"Final score: {result.final_score}")
        print(f"Confidence: {result.confidence}")
        print(f"Prior mock-weather fruiting score: {mock_result.fruiting_score}")
        if result.fruiting_score is not None and mock_result.fruiting_score is not None:
            print(
                "Fruiting-score difference (MESAN - mock): "
                f"{result.fruiting_score - mock_result.fruiting_score:+.6f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
