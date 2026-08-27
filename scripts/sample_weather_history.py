"""Display complete and incomplete aggregate windows from the MESAN archive."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from fungifind.data_sources.smhi_mesan import (
    MesanGridPoint,
    format_utc_datetime,
    parse_utc_datetime,
)
from fungifind.data_sources.weather_history import (
    DEFAULT_MESAN_HISTORY_DATABASE,
    MesanHistoryArchive,
    MesanHistoryError,
    get_weather_history_features,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sample coverage-aware aggregates from local MESAN history"
    )
    parser.add_argument("--latitude", type=float, default=59.412)
    parser.add_argument("--longitude", type=float, default=18.132)
    parser.add_argument("--grid-latitude", type=float)
    parser.add_argument("--grid-longitude", type=float)
    parser.add_argument("--target-time")
    parser.add_argument("--significant-rain-threshold-mm", type=float)
    parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_MESAN_HISTORY_DATABASE,
    )
    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = _parser().parse_args()
    if (args.grid_latitude is None) != (args.grid_longitude is None):
        print(
            "ERROR: --grid-latitude and --grid-longitude must be supplied together",
            file=sys.stderr,
        )
        return 2

    archive = MesanHistoryArchive(args.database)
    try:
        if args.grid_latitude is not None:
            grid_point = MesanGridPoint(args.grid_latitude, args.grid_longitude)
            requested_text = "explicit grid coordinate"
        else:
            grid_point = archive.find_nearest_grid_point(args.latitude, args.longitude)
            requested_text = f"{args.latitude}, {args.longitude}"
        earliest, latest = archive.get_time_bounds(grid_point)
        if latest is None:
            raise MesanHistoryError("Selected grid point contains no stored hours")
        target_time = (
            parse_utc_datetime(args.target_time, "target time")
            if args.target_time is not None
            else latest
        )
        features = get_weather_history_features(
            grid_point,
            target_time,
            archive_path=args.database,
            significant_rain_threshold_mm=args.significant_rain_threshold_mm,
        )
    except (FileNotFoundError, MesanHistoryError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Requested coordinate: {requested_text}")
    print(f"MESAN grid coordinate: {grid_point.latitude}, {grid_point.longitude}")
    print(f"Database: {archive.path}")
    print(
        "Earliest stored time: "
        + (format_utc_datetime(earliest) if earliest is not None else "None")
    )
    print(f"Latest stored time: {format_utc_datetime(latest)}")
    print(f"Target time: {format_utc_datetime(features.target_time)}")
    print(
        "Significant-rain threshold: "
        + (
            f"{features.significant_rain_threshold_mm} mm per hour"
            if features.significant_rain_threshold_mm is not None
            else "not supplied; dry-period metrics disabled"
        )
    )
    print("\nAGGREGATES")
    for name, aggregate in features.aggregates():
        value = "None" if aggregate.value is None else f"{aggregate.value:.6g}"
        print(f"\n{name}: {value} {aggregate.unit}")
        print(f"  status: {aggregate.status.value}")
        print(f"  expected hours: {aggregate.expected_hours}")
        print(f"  available hours: {aggregate.available_hours}")
        print(f"  coverage: {aggregate.coverage_fraction:.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

