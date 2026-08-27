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
    NmdLandcoverRasterDataSource,
    SguSoilVectorDataSource,
    StaticWetnessRasterDataSource,
    TerrainDemReader,
    VectorPointError,
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
    parser.add_argument(
        "--elevation-manifest",
        type=Path,
        default=Path("src/data/elevation/manifest.json"),
    )
    parser.add_argument(
        "--landcover-raster",
        type=Path,
        default=None,
        help=(
            "NMD2023 v2.1 raster. By default, use src/data/landcover and then "
            "the existing src/data/base_layer location."
        ),
    )
    parser.add_argument(
        "--soil-geopackage",
        type=Path,
        default=None,
        help=(
            "SGU Jordarter GeoPackage. By default, use src/data/soil and then "
            "the existing src/data/soil_type location."
        ),
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
    landcover_source = NmdLandcoverRasterDataSource.nmd2023_v2_1(args.landcover_raster)
    try:
        soil_source = SguSoilVectorDataSource.official(args.soil_geopackage)
        terrain_reader = TerrainDemReader.from_manifest(args.elevation_manifest)
        trees = tree_source.sample_profile(location)
        structure = structure_source.sample_features(location)
        wetness = wetness_source.sample_wetness(location)
        landcover = landcover_source.sample_landcover(location)
        terrain = terrain_reader.sample_terrain(location)
        soil = soil_source.sample_soil(location)
    except (
        FileNotFoundError,
        RasterPointError,
        RasterioError,
        ValueError,
        VectorPointError,
    ) as exc:
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

    landcover_item = landcover.snapshot.feature_provenance["landcover_class"]
    landcover_features = landcover.snapshot.features
    print("\nLAND COVER")
    print(f"\nRaw class: {landcover_item.raw_value}")
    print(f"Official label: {landcover_features.landcover_label}")
    print(f"Semantic status: {landcover_item.semantic_status}")
    searchable = landcover_item.details["searchable_habitat"]
    print(f"Searchable habitat: {searchable}")
    if landcover.exclusion_reason is None:
        exclusion_text = "none"
    else:
        exclusion_text = f"{landcover.exclusion_reason[0]} ({landcover.exclusion_reason[1]})"
    print(f"Exclusion reason: {exclusion_text}")
    print(f"CRS: {_crs_label(landcover.sample.source_epsg, landcover.sample.source_crs)}")
    print(f"Pixel: row={landcover.sample.pixel_row}, col={landcover.sample.pixel_col}")
    print(f"NoData: {str(landcover_item.is_nodata).lower()}")

    terrain_features = terrain.snapshot.features
    terrain_match = terrain.tile_match
    print("\nTERRAIN")
    print(f"\nElevation: {terrain_features.elevation_m}")
    print(f"Slope: {terrain_features.slope_degrees}")
    print(f"Aspect: {terrain_features.aspect_degrees}")
    print(
        f"DEM tile: {terrain_match.tile.source_filename} "
        f"(item {terrain_match.tile.item_id})"
    )
    print(f"CRS: {_crs_label(terrain_match.source_epsg, terrain_match.source_crs)}")
    print(
        "CRS components: "
        f"horizontal EPSG:{terrain_match.horizontal_epsg}, "
        f"vertical EPSG:{terrain_match.vertical_epsg}"
    )
    print(f"Method: {terrain.method}")

    soil_features = soil.snapshot.features
    soil_item = soil.snapshot.feature_provenance["soil_type_code"]
    soil_sample = soil.sample
    print("\nSOIL")
    print(f"\nRaw code: {soil_item.raw_value}")
    print(f"Official label: {soil_features.soil_type_label}")
    print(f"Derived group: {soil_features.soil_group}")
    print(f"Semantic status: {soil_item.semantic_status}")
    print(f"Dataset: {soil_item.details['dataset']}")
    print(f"Layer: {soil_sample.layer_name}")
    print(f"Feature ID: {soil_sample.feature_id}")
    print(f"CRS: {_crs_label(soil_sample.source_epsg, soil_sample.source_crs)}")
    print(f"Lookup method: {soil_sample.lookup_method}")

    all_provenance = {
        **trees.snapshot.feature_provenance,
        **structure.snapshot.feature_provenance,
        **wetness.snapshot.feature_provenance,
        **landcover.snapshot.feature_provenance,
        **terrain.snapshot.feature_provenance,
        **soil.snapshot.feature_provenance,
    }
    overall_alignment = diagnose_grid_alignment(all_provenance)
    print("\nTree species grid alignment: exact")
    print(f"Forest structure grid alignment: {structure.grid_alignment.status}")
    print(f"Grid alignment: {overall_alignment.status}")
    print("Each raster and vector source transformed WGS84 independently.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
