"""Append the current SMHI MESAN point window to the local SQLite archive."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from fungifind.data_sources.smhi_mesan import MesanClientError, format_utc_datetime
from fungifind.data_sources.weather_history import (
    DEFAULT_MESAN_HISTORY_DATABASE,
    ingest_recent_mesan,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Update the local MESAN hourly archive")
    parser.add_argument("--latitude", type=float, required=True)
    parser.add_argument("--longitude", type=float, required=True)
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
    try:
        result = ingest_recent_mesan(
            args.latitude,
            args.longitude,
            archive_path=args.database,
        )
    except (MesanClientError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    stats = result.statistics
    print(
        "Requested coordinate: "
        f"{result.requested_point.latitude}, {result.requested_point.longitude}"
    )
    print(
        "MESAN grid coordinate: "
        f"{result.grid_point.latitude}, {result.grid_point.longitude}"
    )
    print(f"Fetched timesteps: {stats.fetched}")
    print(f"Inserted: {stats.inserted}")
    print(f"Already present: {stats.already_present}")
    print(f"Invalid: {stats.invalid}")
    print(f"Database: {result.database_path}")
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

