"""Backfill point history from SMHI's official MESAN GRIDPP GRIB2 archive."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from fungifind.data_sources.smhi_mesan import format_utc_datetime, parse_utc_datetime
from fungifind.data_sources.smhi_mesan_archive import (
    DEFAULT_MESAN_ARCHIVE_CACHE,
    HistoricalMesanError,
)
from fungifind.data_sources.weather_history import (
    DEFAULT_MESAN_HISTORY_DATABASE,
    MesanHistoryError,
    backfill_mesan_history,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill an hourly point archive from SMHI MESAN GRIDPP feed 9. "
            "Complete fields are not downloaded; validated byte ranges are cached."
        )
    )
    parser.add_argument("--latitude", type=float, required=True)
    parser.add_argument("--longitude", type=float, required=True)
    parser.add_argument("--days", type=int, required=True)
    parser.add_argument(
        "--end-time",
        help="Inclusive UTC target hour; defaults to the latest stored live hour",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_MESAN_HISTORY_DATABASE,
    )
    parser.add_argument(
        "--cache-directory",
        type=Path,
        default=DEFAULT_MESAN_ARCHIVE_CACHE,
    )
    parser.add_argument("--workers", type=int, default=8)
    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = _parser().parse_args()
    try:
        end_time = (
            parse_utc_datetime(args.end_time, "--end-time")
            if args.end_time is not None
            else None
        )
        result = backfill_mesan_history(
            args.latitude,
            args.longitude,
            days=args.days,
            archive_path=args.database,
            cache_path=args.cache_directory,
            end_time=end_time,
            max_workers=args.workers,
        )
    except (
        HistoricalMesanError,
        MesanHistoryError,
        FileNotFoundError,
        OSError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    statistics = result.statistics
    print(
        "Requested coordinate: "
        f"{result.requested_point.latitude}, {result.requested_point.longitude}"
    )
    print(
        "Historical grid coordinate: "
        f"{result.grid_point.latitude:.9f}, {result.grid_point.longitude:.9f}"
    )
    print(
        "Requested period: "
        f"{format_utc_datetime(result.requested_start_time)} to "
        f"{format_utc_datetime(result.requested_end_time)} inclusive"
    )
    print(f"Records discovered: {statistics.discovered}")
    print(f"Records parsed: {statistics.parsed}")
    print(f"Records inserted: {statistics.inserted}")
    print(f"Records already present: {statistics.already_present}")
    print(f"Records invalid: {statistics.invalid}")
    print(f"Records unavailable: {statistics.unavailable}")
    print(f"Grid identity: {result.grid_identity}")
    print(
        "Grid resolution: "
        f"{result.grid_resolution_m[0]:.3f} x {result.grid_resolution_m[1]:.3f} m"
    )
    print(f"Sampling: {result.sampling_method}")
    print(f"Database: {result.database_path}")
    print(f"Cache: {result.cache_path}")
    print(f"Precedence: {result.precedence_policy}")
    print(f"Overlap records compared: {len(result.overlap_differences)}")
    for difference in result.overlap_differences:
        print(
            "  "
            f"{format_utc_datetime(difference.time)}: "
            f"temperature {difference.air_temperature_difference_c:+.6f} °C, "
            f"precipitation {difference.precipitation_1h_difference_mm:+.6f} mm, "
            f"RH {difference.relative_humidity_difference_percent:+.6f} percentage points, "
            f"wind {difference.wind_speed_difference_m_s:+.6f} m/s"
        )
    print(
        "Earliest stored time: "
        + (
            format_utc_datetime(result.earliest_stored_time)
            if result.earliest_stored_time is not None
            else "None"
        )
    )
    print(
        "Latest stored time: "
        + (
            format_utc_datetime(result.latest_stored_time)
            if result.latest_stored_time is not None
            else "None"
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
