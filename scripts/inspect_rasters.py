"""Command-line entry point for memory-safe GeoTIFF inspection."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rasterio.errors import RasterioError

from fungifind.raster_inspection import (
    InspectionOptions,
    discover_rasters,
    format_terminal_report,
    inspect_raster,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect one GeoTIFF or every GeoTIFF in a directory without modifying them."
    )
    parser.add_argument("input", type=Path, help="A .tif/.tiff file or a directory")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("reports/raster_inspection"),
        help="Report directory (default: reports/raster_inspection)",
    )
    parser.add_argument(
        "--exact-pixel-limit",
        type=int,
        default=10_000_000,
        help="Maximum total source pixels for a streamed exact scan",
    )
    parser.add_argument(
        "--sample-pixels",
        type=int,
        default=1_000_000,
        help="Maximum representative pixels read per band for large rasters",
    )
    return parser


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    try:
        rasters = discover_rasters(args.input)
        options = InspectionOptions(
            exact_scan_pixel_limit=args.exact_pixel_limit,
            max_sample_pixels=args.sample_pixels,
        )
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))

    failures = 0
    for raster_path in rasters:
        try:
            report = inspect_raster(raster_path, args.output_dir, options)
        except (OSError, RasterioError, ValueError) as exc:
            failures += 1
            print(f"ERROR {raster_path}: {exc}", file=sys.stderr)
            continue
        print(format_terminal_report(report))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
