"""Read and print one raw GeoTIFF pixel selected by a WGS84 coordinate."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rasterio.errors import RasterioError

from fungifind.data_sources.raster import RasterPointError, RasterPointReader
from fungifind.models import Location


def main() -> int:
    parser = argparse.ArgumentParser(description="Read one GeoTIFF pixel from WGS84 input")
    parser.add_argument("raster", type=Path)
    parser.add_argument("--latitude", type=float, required=True)
    parser.add_argument("--longitude", type=float, required=True)
    parser.add_argument("--band", type=int, default=1)
    args = parser.parse_args()

    location = Location(latitude=args.latitude, longitude=args.longitude)
    try:
        sample = RasterPointReader(args.raster, band=args.band).sample(location)
    except (FileNotFoundError, RasterPointError, RasterioError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Raster: {Path(sample.source_path).name}")
    print(f"Input WGS84: {location.latitude}, {location.longitude}")
    print(f"Raster CRS: {sample.source_crs} (EPSG: {sample.source_epsg})")
    print(f"Projected coordinate: x={sample.projected_x:.3f}, y={sample.projected_y:.3f}")
    print(f"Pixel: row={sample.pixel_row}, col={sample.pixel_col}")
    print(f"Raw value: {sample.raw_value}")
    print(f"Value: {sample.value}")
    print(f"NoData: {str(sample.is_nodata).lower()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

