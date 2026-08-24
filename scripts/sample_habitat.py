"""Sample real tree-species and unvalidated forest-structure rasters."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rasterio.errors import RasterioError

from fungifind.data_sources import (
    ConfiguredRasterFeatureDataSource,
    ForestProfileRasterDataSource,
    ForestShareInterpretation,
    StaticWetnessRasterDataSource,
    diagnose_grid_alignment,
)
from fungifind.data_sources.raster import RasterPointError
from fungifind.models import Location

TREE_LABELS = {
    "spruce_fraction": "Spruce",
    "pine_fraction": "Pine",
    "birch_fraction": "Birch",
    "other_deciduous_fraction": "Other deciduous",
}


def _crs_label(epsg: int | None, crs: str) -> str:
    return f"EPSG:{epsg}" if epsg is not None else crs


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Sample real forest habitat rasters")
    parser.add_argument("--latitude", type=float, required=True)
    parser.add_argument("--longitude", type=float, required=True)
    parser.add_argument("--tree-dir", type=Path, default=Path("src/data/kind"))
    parser.add_argument("--structure-dir", type=Path, default=Path("src/data/misc_data"))
    parser.add_argument(
        "--wetness-raster",
        type=Path,
        default=Path("src/data/misc_data/SLUMarkfuktighetKlassad.tif"),
    )
    args = parser.parse_args()
    location = Location(args.latitude, args.longitude)

    tree_source = ForestProfileRasterDataSource.from_kind_directory(
        args.tree_dir,
        interpretation=ForestShareInterpretation.PROVISIONAL_ZERO_TO_HUNDRED_SHARE,
    )
    structure_source = ConfiguredRasterFeatureDataSource.forest_structure(
        args.structure_dir
    )
    wetness_source = StaticWetnessRasterDataSource.slu_classified(args.wetness_raster)
    try:
        trees = tree_source.sample_profile(location)
        structure = structure_source.sample_features(location)
        wetness = wetness_source.sample_wetness(location)
    except (FileNotFoundError, RasterPointError, RasterioError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Location: {location.latitude}, {location.longitude}\n")
    print("TREE SPECIES\n")
    for name, label in TREE_LABELS.items():
        item = trees.snapshot.feature_provenance[name]
        print(f"{label}: raw={item.raw_value}, preliminary={item.interpreted_value}")

    print("\nFOREST STRUCTURE")
    for config in structure_source.configs:
        item = structure.snapshot.feature_provenance[config.target_feature]
        sample = structure.samples[config.target_feature]
        print(f"\n{config.label}:")
        print(f"  raw: {item.raw_value}")
        print(f"  interpreted: {item.interpreted_value}")
        print(f"  semantic status: {item.semantic_status}")
        print(f"  NoData: {str(item.is_nodata).lower()}")
        print(f"  CRS: {_crs_label(sample.source_epsg, sample.source_crs)}")
        print(f"  pixel: row={sample.pixel_row}, col={sample.pixel_col}")

    wetness_item = wetness.snapshot.feature_provenance["static_wetness_class"]
    print("\nSTATIC WETNESS")
    print(f"\nRaw class: {wetness_item.raw_value}")
    interpreted = wetness.snapshot.features.static_wetness_class
    label = wetness.snapshot.features.static_wetness_label
    interpreted_text = "None" if interpreted is None else f"{interpreted} ({label})"
    print(f"Interpreted class: {interpreted_text}")
    print(f"Semantic status: {wetness_item.semantic_status}")
    print(f"CRS: {_crs_label(wetness.sample.source_epsg, wetness.sample.source_crs)}")
    print(f"Pixel: row={wetness.sample.pixel_row}, col={wetness.sample.pixel_col}")
    print(f"NoData: {str(wetness_item.is_nodata).lower()}")

    all_provenance = {
        **trees.snapshot.feature_provenance,
        **structure.snapshot.feature_provenance,
        **wetness.snapshot.feature_provenance,
    }
    overall_alignment = diagnose_grid_alignment(all_provenance)
    print("\nTree species grid alignment: exact")
    print(f"Forest structure grid alignment: {structure.grid_alignment.status}")
    print(f"Grid alignment: {overall_alignment.status}")
    print("Each raster was transformed and indexed independently from WGS84.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
