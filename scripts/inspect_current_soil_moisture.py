"""Inspect the moisture heuristic at five reproducible real habitat points."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path

from fungifind.data_sources import (
    CompositeHabitatDataSource,
    ElevationTileNotFoundError,
    MesanWeatherHistoryDataSource,
    NmdLandcoverRasterDataSource,
    SguSoilVectorDataSource,
    StaticWetnessRasterDataSource,
    TerrainDemReader,
)
from fungifind.models import (
    DataSourceMetadata,
    FeatureSnapshot,
    Location,
    StaticHabitatFeatures,
)
from fungifind.moisture import CurrentSoilMoistureEstimator

DEFAULT_TARGET_DATE = date(2026, 8, 27)
DEFAULT_REPORT = Path("reports/current_soil_moisture_real_points.json")

# These coordinates were selected inside the two locally backfilled MESAN grid
# catchments. Their source classes are read afresh; the labels describe only the
# intended contrast and are not substituted for source data.
REAL_TEST_POINTS = {
    "dry_coarse_mineral": Location(59.166136, 18.242348),
    "mesic_moraine": Location(59.160136, 18.247348),
    "wet_fine_mineral": Location(59.163136, 18.247348),
    "peat": Location(59.139136, 18.237348),
    "sloped_bedrock": Location(59.430000, 18.080000),
}


class _OptionalTerrainDataSource:
    """Expose a missing optional slope outside the downloaded DEM tile."""

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
                    is_mock=False,
                    details={"optional_slope_status": "no_local_dem_tile"},
                ),
            )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run current-soil-moisture v1 on five real local habitat points"
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
            "static_wetness": StaticWetnessRasterDataSource.slu_classified(
                "src/data/misc_data/SLUMarkfuktighetKlassad.tif"
            ),
            "landcover": NmdLandcoverRasterDataSource.nmd2023_v2_1(),
            "soil": SguSoilVectorDataSource.official(),
            "terrain": _OptionalTerrainDataSource(elevation_manifest),
        }
    )


def _point_report(
    *,
    label: str,
    location: Location,
    target_date: date,
    habitat_source: CompositeHabitatDataSource,
    weather_source: MesanWeatherHistoryDataSource,
    estimator: CurrentSoilMoistureEstimator,
) -> dict[str, object]:
    habitat = habitat_source.get_features(location)
    weather = weather_source.get_features(location, target_date)
    estimate = estimator.estimate(habitat, weather)
    if estimate.estimated_current_soil_moisture is None:
        raise RuntimeError(f"{label} did not produce an estimate: {estimate.status}")

    habitat_features = habitat.features
    weather_features = weather.features
    weather_details = weather.metadata.details
    return {
        "label": label,
        "latitude": location.latitude,
        "longitude": location.longitude,
        "target_date": target_date.isoformat(),
        "mesan_grid_latitude": weather_details["grid_latitude"],
        "mesan_grid_longitude": weather_details["grid_longitude"],
        "mesan_grid_distance_m": weather_details["grid_distance_m"],
        "inputs": {
            "static_wetness_class": habitat_features.static_wetness_class,
            "soil_group": habitat_features.soil_group,
            "slope_degrees": habitat_features.slope_degrees,
            "rain_3d_mm": weather_features.rainfall_3d_mm,
            "rain_7d_mm": weather_features.rainfall_7d_mm,
            "rain_14d_mm": weather_features.rainfall_14d_mm,
            "rain_30d_mm": weather_features.rainfall_30d_mm,
            "temp_mean_7d_c": weather_features.temp_mean_7d_c,
            "relative_humidity_mean_7d": (
                weather_features.relative_humidity_mean_7d_percent
            ),
        },
        "moisture": asdict(estimate),
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = _parser().parse_args()
    try:
        habitat_source = _habitat_source(args.elevation_manifest)
        weather_source = MesanWeatherHistoryDataSource(args.database)
        estimator = CurrentSoilMoistureEstimator()
        payload = {
            "estimator_version": "current_soil_moisture_heuristic_v1",
            "target_date": args.date.isoformat(),
            "point_count": len(REAL_TEST_POINTS),
            "points": [
                _point_report(
                    label=label,
                    location=location,
                    target_date=args.date,
                    habitat_source=habitat_source,
                    weather_source=weather_source,
                    estimator=estimator,
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
