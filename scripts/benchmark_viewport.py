"""Benchmark representative production-v1 viewport grids with stage timings."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import date
from pathlib import Path
from time import perf_counter

from pyproj import Transformer

from fungifind.api.app import build_scoring_service
from fungifind.models import Species
from fungifind.viewport import (
    TtlLruCache,
    ViewportBatchEvaluator,
    ViewportBounds,
    build_aligned_grid,
)

_TO_SWEREF = Transformer.from_crs("EPSG:4326", "EPSG:3006", always_xy=True)
_TO_WGS84 = Transformer.from_crs("EPSG:3006", "EPSG:4326", always_xy=True)
CASES = (
    ("1x1_km_at_50m", 1_000, 50),
    ("5x5_km_at_100m", 5_000, 100),
    ("10x10_km_at_200m", 10_000, 200),
)


def _bounds(latitude: float, longitude: float, size_m: int) -> ViewportBounds:
    center_x, center_y = _TO_SWEREF.transform(longitude, latitude)
    half = size_m / 2
    xs = [center_x - half, center_x + half, center_x + half, center_x - half]
    ys = [center_y - half, center_y - half, center_y + half, center_y + half]
    longitudes, latitudes = _TO_WGS84.transform(xs, ys)
    return ViewportBounds(
        west=min(longitudes),
        south=min(latitudes),
        east=max(longitudes),
        north=max(latitudes),
    )


def _source_timings_ms(service, locations) -> dict[str, float]:
    labels = {
        "landcover": "nmd",
        "static_wetness": "slu_wetness",
        "tree_species": "tree_rasters",
        "soil": "sgu",
        "terrain": "dem",
    }
    timings: dict[str, float] = {}
    for source_name, label in labels.items():
        source = service.habitat_source.sources[source_name]
        started = perf_counter()
        batch_method = getattr(source, "get_features_many", None)
        if callable(batch_method):
            tuple(batch_method(locations))
        else:
            tuple(source.get_features(location) for location in locations)
        timings[label] = round((perf_counter() - started) * 1000, 3)
    timings["moisture"] = 0.0
    return timings


def run_benchmarks(
    *,
    latitude: float,
    longitude: float,
    target_date: date,
    species: Species,
) -> dict[str, object]:
    results: list[dict[str, object]] = []
    for name, size_m, resolution_m in CASES:
        grid_started = perf_counter()
        grid = build_aligned_grid(
            _bounds(latitude, longitude, size_m), resolution_m
        )
        grid_ms = (perf_counter() - grid_started) * 1000
        locations = [cell.center for cell in grid.cells]

        profile_service = build_scoring_service()
        source_timings = _source_timings_ms(profile_service, locations)

        service = build_scoring_service()
        evaluator = ViewportBatchEvaluator(service)
        cold_started = perf_counter()
        evaluation = evaluator.evaluate(grid, species, target_date)
        cold_ms = (perf_counter() - cold_started) * 1000

        warm_started = perf_counter()
        warm_evaluation = evaluator.evaluate(grid, species, target_date)
        warm_ms = (perf_counter() - warm_started) * 1000
        if warm_evaluation.features != evaluation.features:
            raise RuntimeError("Cold and warm viewport output differ")

        cache: TtlLruCache[object] = TtlLruCache(max_entries=1)
        cache.set("viewport", evaluation, ttl_seconds=60)
        cache_started = perf_counter()
        cached = cache.get("viewport")
        cache_lookup_ms = (perf_counter() - cache_started) * 1000
        if cached is None:
            raise RuntimeError("Benchmark cache lookup unexpectedly missed")

        payload_started = perf_counter()
        payload = json.dumps(
            {"type": "FeatureCollection", "features": evaluation.features},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        payload_ms = (perf_counter() - payload_started) * 1000
        results.append(
            {
                "case": name,
                "requested_size_m": size_m,
                "requested_resolution_m": resolution_m,
                "actual_resolution_m": grid.resolution_m,
                "grid_cell_count": grid.cell_count,
                "eligible_habitat_cell_count": (
                    evaluation.eligible_habitat_cell_count
                ),
                "feature_count": len(evaluation.features),
                "excluded_cell_count": evaluation.excluded_cell_count,
                "no_data_cell_count": evaluation.no_data_cell_count,
                "unique_mesan_point_count": evaluation.unique_mesan_point_count,
                "payload_bytes": len(payload),
                "cold_time_ms": round(cold_ms, 3),
                "warm_time_ms": round(warm_ms, 3),
                "cold_cells_per_second": round(
                    grid.cell_count / (cold_ms / 1000), 2
                ),
                "warm_cells_per_second": round(
                    grid.cell_count / (warm_ms / 1000), 2
                ),
                "source_profile_ms": {
                    **source_timings,
                    "mesan": evaluation.timings.weather_ms,
                    "scoring": evaluation.timings.scoring_ms,
                },
                "timings_ms": {
                    "grid": round(grid_ms, 3),
                    **asdict(evaluation.timings),
                    "json_encoding": round(payload_ms, 3),
                    "backend_cache_lookup": round(cache_lookup_ms, 6),
                },
            }
        )
    return {
        "benchmark": "fungifind_viewport_overlay_v1",
        "model_version": "production_v1",
        "species": species.value,
        "date": target_date.isoformat(),
        "center": {"latitude": latitude, "longitude": longitude},
        "cases": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--latitude", type=float, default=59.160136)
    parser.add_argument("--longitude", type=float, default=18.247348)
    parser.add_argument("--date", type=date.fromisoformat, default=date(2026, 8, 27))
    parser.add_argument(
        "--species",
        type=Species,
        choices=list(Species),
        default=Species.CANTHARELLUS_CIBARIUS,
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run_benchmarks(
        latitude=args.latitude,
        longitude=args.longitude,
        target_date=args.date,
        species=args.species,
    )
    serialized = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")


if __name__ == "__main__":
    main()
