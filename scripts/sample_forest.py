"""Sample the configured four-raster tree-species profile at one WGS84 location."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rasterio.errors import RasterioError

from fungifind.data_sources import ForestProfileRasterDataSource, ForestShareInterpretation
from fungifind.data_sources.raster import RasterPointError
from fungifind.models import Location

LABELS = {
    "spruce_fraction": "Spruce",
    "pine_fraction": "Pine",
    "birch_fraction": "Birch",
    "other_deciduous_fraction": "Other deciduous",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Sample the four tree-species rasters")
    parser.add_argument("--latitude", type=float, required=True)
    parser.add_argument("--longitude", type=float, required=True)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("src/data/kind"),
        help="Directory containing Gran/Tall/Bjork/OvrLov_andel.tif",
    )
    args = parser.parse_args()

    location = Location(args.latitude, args.longitude)
    source = ForestProfileRasterDataSource.from_kind_directory(
        args.data_dir,
        interpretation=ForestShareInterpretation.PROVISIONAL_ZERO_TO_HUNDRED_SHARE,
    )
    try:
        result = source.sample_profile(location)
    except (FileNotFoundError, RasterPointError, RasterioError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Location: {location.latitude}, {location.longitude}\n")
    for name, label in LABELS.items():
        item = result.snapshot.feature_provenance[name]
        print(f"{label} raw: {item.raw_value} (NoData: {str(item.semantic_status == 'nodata').lower()})")

    print("\nPreliminary fractions:")
    for name, label in LABELS.items():
        print(f"{label.lower()}: {getattr(result.snapshot.features, name)}")

    diagnostics = result.diagnostics
    print(f"\nSum: {diagnostics.tree_fraction_sum}")
    print(f"Sum status: {diagnostics.status}")
    print(f"Near 1.0: {str(diagnostics.is_near_one).lower()}")
    print(f"Clearly below 1.0: {str(diagnostics.is_clearly_below_one).lower()}")
    print(f"Exceeds 1.0: {str(diagnostics.exceeds_one).lower()}")
    print(f"Missing features: {', '.join(diagnostics.missing_features) or 'none'}")
    print(f"NoData features: {', '.join(diagnostics.nodata_features) or 'none'}")

    crs_values = sorted(
        {str(item.details["source_crs"]) for item in result.snapshot.feature_provenance.values()}
    )
    pixel_values = sorted(
        {
            (int(item.details["pixel_row"]), int(item.details["pixel_col"]))
            for item in result.snapshot.feature_provenance.values()
        }
    )
    print(f"\nCRS: {', '.join(crs_values)}")
    print(
        "Pixel coordinates: "
        + ", ".join(f"row={row}, col={col}" for row, col in pixel_values)
    )
    print("Interpretation: provisional 0-100 to 0-1; semantics not officially validated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
