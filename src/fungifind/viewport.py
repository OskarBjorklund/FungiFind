"""Deterministic SWEREF 99 TM viewport grid and production-v1 batch evaluation."""

from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date
from threading import RLock
from time import monotonic, perf_counter
from typing import Any, Generic, TypeVar

from pyproj import Transformer

from fungifind.data_sources.mesan_weather import MesanWeatherHistoryDataSource
from fungifind.data_sources.raster import RasterPointOutsideBoundsError
from fungifind.data_sources.weather_history import MesanHistoryError
from fungifind.models import FeatureSnapshot, Location, Species, StaticHabitatFeatures
from fungifind.scoring import collect_habitat_exclusions
from fungifind.service import MushroomScoringService

WGS84_EPSG = 4326
SWEREF99_TM_EPSG = 3006
REQUESTED_RESOLUTIONS_M = (25, 50, 100, 200)
MAX_GRID_CELLS = 10_000
MAX_PROJECTED_SPAN_M = 100_000.0
MAX_PROJECTED_AREA_M2 = 10_000_000_000.0
MAX_AUTO_RESOLUTION_M = 6_400
VIEWPORT_MODEL_VERSION = "production_v1"
VIEWPORT_CONFIG_VERSION = "viewport_overlay_v1"

_TO_SWEREF = Transformer.from_crs(
    f"EPSG:{WGS84_EPSG}",
    f"EPSG:{SWEREF99_TM_EPSG}",
    always_xy=True,
)
_TO_WGS84 = Transformer.from_crs(
    f"EPSG:{SWEREF99_TM_EPSG}",
    f"EPSG:{WGS84_EPSG}",
    always_xy=True,
)


class ViewportValidationError(ValueError):
    """A stable validation failure suitable for a clear API 4xx response."""

    def __init__(self, code: str, message: str, *, field: str) -> None:
        super().__init__(message)
        self.code = code
        self.field = field


@dataclass(frozen=True, slots=True)
class ViewportBounds:
    west: float
    south: float
    east: float
    north: float

    def __post_init__(self) -> None:
        values = (self.west, self.south, self.east, self.north)
        if not all(math.isfinite(value) for value in values):
            raise ViewportValidationError(
                "invalid_bbox", "bbox values must be finite numbers", field="bbox"
            )
        if not -180 <= self.west < self.east <= 180:
            raise ViewportValidationError(
                "invalid_bbox",
                "bbox west/east must be ordered within -180..180",
                field="bbox",
            )
        if not -90 <= self.south < self.north <= 90:
            raise ViewportValidationError(
                "invalid_bbox",
                "bbox south/north must be ordered within -90..90",
                field="bbox",
            )

    def as_list(self) -> list[float]:
        return [self.west, self.south, self.east, self.north]


@dataclass(frozen=True, slots=True)
class ViewportCell:
    cell_id: str
    center: Location
    projected_bounds: tuple[int, int, int, int]
    polygon: tuple[tuple[float, float], ...]


@dataclass(frozen=True, slots=True)
class AlignedViewportGrid:
    requested_resolution_m: int
    resolution_m: int
    projected_bounds: tuple[int, int, int, int]
    coverage_bounds: ViewportBounds
    columns: int
    rows: int
    cells: tuple[ViewportCell, ...]

    @property
    def cell_count(self) -> int:
        return self.columns * self.rows

    @property
    def cache_identity(self) -> tuple[int, int, int, int, int]:
        return (self.resolution_m, *self.projected_bounds)


@dataclass(frozen=True, slots=True)
class ViewportStageTimings:
    grid_ms: float = 0.0
    habitat_ms: float = 0.0
    mesan_resolve_ms: float = 0.0
    weather_ms: float = 0.0
    scoring_ms: float = 0.0
    serialization_ms: float = 0.0
    total_ms: float = 0.0


@dataclass(frozen=True, slots=True)
class ViewportEvaluation:
    features: tuple[dict[str, Any], ...]
    eligible_habitat_cell_count: int
    excluded_cell_count: int
    no_data_cell_count: int
    unique_mesan_point_count: int
    timings: ViewportStageTimings


def parse_bbox(value: str) -> ViewportBounds:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 4:
        raise ViewportValidationError(
            "invalid_bbox",
            "bbox must contain west,south,east,north",
            field="bbox",
        )
    try:
        west, south, east, north = (float(part) for part in parts)
    except ValueError as exc:
        raise ViewportValidationError(
            "invalid_bbox", "bbox values must be numbers", field="bbox"
        ) from exc
    return ViewportBounds(west=west, south=south, east=east, north=north)


def _projected_envelope(bounds: ViewportBounds) -> tuple[float, float, float, float]:
    longitudes = [bounds.west, bounds.east, bounds.east, bounds.west]
    latitudes = [bounds.south, bounds.south, bounds.north, bounds.north]
    xs, ys = _TO_SWEREF.transform(longitudes, latitudes)
    if not all(math.isfinite(value) for value in (*xs, *ys)):
        raise ViewportValidationError(
            "invalid_bbox",
            "bbox cannot be transformed to EPSG:3006",
            field="bbox",
        )
    return min(xs), min(ys), max(xs), max(ys)


def _aligned_shape(
    envelope: tuple[float, float, float, float], resolution_m: int
) -> tuple[int, int, int, int, int, int]:
    min_x, min_y, max_x, max_y = envelope
    start_x = math.floor(min_x / resolution_m) * resolution_m
    start_y = math.floor(min_y / resolution_m) * resolution_m
    end_x = math.ceil(max_x / resolution_m) * resolution_m
    end_y = math.ceil(max_y / resolution_m) * resolution_m
    columns = (end_x - start_x) // resolution_m
    rows = (end_y - start_y) // resolution_m
    return start_x, start_y, end_x, end_y, columns, rows


def build_aligned_grid(
    bounds: ViewportBounds,
    requested_resolution_m: int,
    *,
    max_cells: int = MAX_GRID_CELLS,
) -> AlignedViewportGrid:
    """Build globally aligned square cells and deterministically auto-coarsen."""

    if requested_resolution_m not in REQUESTED_RESOLUTIONS_M:
        allowed = ", ".join(str(item) for item in REQUESTED_RESOLUTIONS_M)
        raise ViewportValidationError(
            "invalid_resolution",
            f"resolution must be one of {allowed} metres",
            field="resolution",
        )
    if max_cells < 1:
        raise ValueError("max_cells must be positive")

    envelope = _projected_envelope(bounds)
    span_x = envelope[2] - envelope[0]
    span_y = envelope[3] - envelope[1]
    if (
        span_x > MAX_PROJECTED_SPAN_M
        or span_y > MAX_PROJECTED_SPAN_M
        or span_x * span_y > MAX_PROJECTED_AREA_M2
    ):
        raise ViewportValidationError(
            "bbox_too_large",
            "bbox may cover at most 100 km per side and 10,000 km²",
            field="bbox",
        )

    actual_resolution = requested_resolution_m
    while True:
        start_x, start_y, end_x, end_y, columns, rows = _aligned_shape(
            envelope, actual_resolution
        )
        if columns * rows <= max_cells:
            break
        actual_resolution *= 2
        if actual_resolution > MAX_AUTO_RESOLUTION_M:
            raise ViewportValidationError(
                "bbox_too_large",
                f"bbox cannot be reduced below the {max_cells}-cell limit",
                field="bbox",
            )

    projected_rings: list[tuple[int, int, int, int]] = []
    center_xs: list[float] = []
    center_ys: list[float] = []
    corner_xs: list[float] = []
    corner_ys: list[float] = []
    for row in range(rows):
        y0 = start_y + row * actual_resolution
        y1 = y0 + actual_resolution
        for column in range(columns):
            x0 = start_x + column * actual_resolution
            x1 = x0 + actual_resolution
            projected_rings.append((x0, y0, x1, y1))
            center_xs.append(x0 + actual_resolution / 2)
            center_ys.append(y0 + actual_resolution / 2)
            corner_xs.extend((x0, x1, x1, x0, x0))
            corner_ys.extend((y0, y0, y1, y1, y0))

    center_lons, center_lats = _TO_WGS84.transform(center_xs, center_ys)
    corner_lons, corner_lats = _TO_WGS84.transform(corner_xs, corner_ys)
    cells: list[ViewportCell] = []
    for index, (x0, y0, x1, y1) in enumerate(projected_rings):
        corner_offset = index * 5
        polygon = tuple(
            (
                round(float(corner_lons[corner_offset + item]), 7),
                round(float(corner_lats[corner_offset + item]), 7),
            )
            for item in range(5)
        )
        cells.append(
            ViewportCell(
                cell_id=f"{actual_resolution}:{x0}:{y0}",
                center=Location(
                    latitude=float(center_lats[index]),
                    longitude=float(center_lons[index]),
                ),
                projected_bounds=(x0, y0, x1, y1),
                polygon=polygon,
            )
        )

    coverage_lons, coverage_lats = _TO_WGS84.transform(
        [start_x, end_x, end_x, start_x],
        [start_y, start_y, end_y, end_y],
    )
    coverage = ViewportBounds(
        west=min(coverage_lons),
        south=min(coverage_lats),
        east=max(coverage_lons),
        north=max(coverage_lats),
    )
    return AlignedViewportGrid(
        requested_resolution_m=requested_resolution_m,
        resolution_m=actual_resolution,
        projected_bounds=(start_x, start_y, end_x, end_y),
        coverage_bounds=coverage,
        columns=columns,
        rows=rows,
        cells=tuple(cells),
    )


class ViewportBatchEvaluator:
    """Evaluate viewport cells without invoking the point-service pipeline."""

    def __init__(self, service: MushroomScoringService) -> None:
        self.service = service

    @staticmethod
    def _feature(cell: ViewportCell, result: Any) -> dict[str, Any]:
        return {
            "type": "Feature",
            "id": cell.cell_id,
            "geometry": {
                "type": "Polygon",
                "coordinates": [[list(position) for position in cell.polygon]],
            },
            "properties": {
                "cell_id": cell.cell_id,
                "model_version": VIEWPORT_MODEL_VERSION,
                "eligibility": "eligible",
                "final_index": result.final_score,
                "habitat_index": result.habitat_score,
                "fruiting_index": result.fruiting_score,
                "data_confidence": result.confidence,
            },
        }

    def evaluate(
        self,
        grid: AlignedViewportGrid,
        species: Species,
        target_date: date,
    ) -> ViewportEvaluation:
        started = perf_counter()
        habitat_ms = 0.0
        resolve_ms = 0.0
        weather_ms = 0.0
        scoring_ms = 0.0
        serialization_ms = 0.0
        excluded = 0
        no_data = 0
        candidates: list[
            tuple[ViewportCell, FeatureSnapshot[StaticHabitatFeatures]]
        ] = []

        stage = perf_counter()
        batch_method = getattr(
            self.service.habitat_source,
            "get_features_many_eligibility_first",
            None,
        )
        if not callable(batch_method):
            batch_method = getattr(
                self.service.habitat_source, "get_features_many", None
            )
        if callable(batch_method):
            try:
                habitat_batch = tuple(
                    batch_method([cell.center for cell in grid.cells])
                )
            except RasterPointOutsideBoundsError:
                habitat_batch = ()
        else:
            habitat_batch = ()
        if habitat_batch and len(habitat_batch) != len(grid.cells):
            raise ValueError("Habitat batch source returned the wrong item count")
        habitat_ms += (perf_counter() - stage) * 1000

        for index, cell in enumerate(grid.cells):
            if habitat_batch:
                habitat = habitat_batch[index]
            else:
                stage = perf_counter()
                try:
                    habitat = self.service.habitat_source.get_features(cell.center)
                except RasterPointOutsideBoundsError:
                    no_data += 1
                    habitat_ms += (perf_counter() - stage) * 1000
                    continue
                habitat_ms += (perf_counter() - stage) * 1000
            if collect_habitat_exclusions(habitat):
                excluded += 1
                continue
            candidates.append((cell, habitat))

        features: list[dict[str, Any]] = []
        unique_mesan_points = 0
        weather_source = self.service.weather_source
        if isinstance(weather_source, MesanWeatherHistoryDataSource):
            groups: dict[
                tuple[float, float, date],
                list[
                    tuple[
                        ViewportCell,
                        FeatureSnapshot[StaticHabitatFeatures],
                        Any,
                        float,
                    ]
                ],
            ] = {}
            for cell, habitat in candidates:
                stage = perf_counter()
                try:
                    grid_point, distance = weather_source.snap_grid_point(cell.center)
                except MesanHistoryError:
                    no_data += 1
                    resolve_ms += (perf_counter() - stage) * 1000
                    continue
                resolve_ms += (perf_counter() - stage) * 1000
                key = (
                    grid_point.normalized_latitude,
                    grid_point.normalized_longitude,
                    target_date,
                )
                groups.setdefault(key, []).append(
                    (cell, habitat, grid_point, distance)
                )

            unique_mesan_points = len(groups)
            for group in groups.values():
                representative_cell, _, grid_point, distance = group[0]
                stage = perf_counter()
                try:
                    resolution = weather_source.resolve_snapped(
                        representative_cell.center,
                        target_date,
                        grid_point,
                        distance,
                    )
                    weather = weather_source.get_features_for_resolution(resolution)
                except MesanHistoryError:
                    no_data += len(group)
                    weather_ms += (perf_counter() - stage) * 1000
                    continue
                weather_ms += (perf_counter() - stage) * 1000
                for cell, habitat, _, _ in group:
                    stage = perf_counter()
                    try:
                        result = self.service.scoring_engine.score(
                            species,
                            cell.center,
                            target_date,
                            habitat,
                            weather,
                        )
                    except ValueError:
                        no_data += 1
                        scoring_ms += (perf_counter() - stage) * 1000
                        continue
                    scoring_ms += (perf_counter() - stage) * 1000
                    if result.final_score is None:
                        no_data += 1
                        continue
                    stage = perf_counter()
                    features.append(self._feature(cell, result))
                    serialization_ms += (perf_counter() - stage) * 1000
        else:
            # Protocol-compatible fallback for unit tests and alternate local
            # sources. Production uses the grouped MESAN path above.
            unique_mesan_points = len(candidates)
            for cell, habitat in candidates:
                stage = perf_counter()
                try:
                    weather = weather_source.get_features(cell.center, target_date)
                except MesanHistoryError:
                    no_data += 1
                    weather_ms += (perf_counter() - stage) * 1000
                    continue
                weather_ms += (perf_counter() - stage) * 1000
                stage = perf_counter()
                try:
                    result = self.service.scoring_engine.score(
                        species,
                        cell.center,
                        target_date,
                        habitat,
                        weather,
                    )
                except ValueError:
                    no_data += 1
                    scoring_ms += (perf_counter() - stage) * 1000
                    continue
                scoring_ms += (perf_counter() - stage) * 1000
                if result.final_score is None:
                    no_data += 1
                    continue
                stage = perf_counter()
                features.append(self._feature(cell, result))
                serialization_ms += (perf_counter() - stage) * 1000

        total_ms = (perf_counter() - started) * 1000
        return ViewportEvaluation(
            features=tuple(features),
            eligible_habitat_cell_count=len(candidates),
            excluded_cell_count=excluded,
            no_data_cell_count=no_data,
            unique_mesan_point_count=unique_mesan_points,
            timings=ViewportStageTimings(
                habitat_ms=round(habitat_ms, 3),
                mesan_resolve_ms=round(resolve_ms, 3),
                weather_ms=round(weather_ms, 3),
                scoring_ms=round(scoring_ms, 3),
                serialization_ms=round(serialization_ms, 3),
                total_ms=round(total_ms, 3),
            ),
        )


CacheValue = TypeVar("CacheValue")


class TtlLruCache(Generic[CacheValue]):
    """Small thread-safe TTL LRU; values become visible only after full evaluation."""

    def __init__(self, max_entries: int = 32) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self.max_entries = max_entries
        self._lock = RLock()
        self._values: OrderedDict[object, tuple[float, CacheValue]] = OrderedDict()

    def get(self, key: object) -> CacheValue | None:
        now = monotonic()
        with self._lock:
            item = self._values.get(key)
            if item is None:
                return None
            expires_at, value = item
            if expires_at <= now:
                del self._values[key]
                return None
            self._values.move_to_end(key)
            return value

    def set(self, key: object, value: CacheValue, ttl_seconds: float) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        with self._lock:
            self._values[key] = (monotonic() + ttl_seconds, value)
            self._values.move_to_end(key)
            while len(self._values) > self.max_entries:
                self._values.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._values.clear()


def viewport_cache_key(
    grid: AlignedViewportGrid,
    species: Species,
    target_date: date,
) -> tuple[object, ...]:
    return (
        species.value,
        target_date.isoformat(),
        *grid.cache_identity,
        VIEWPORT_MODEL_VERSION,
        VIEWPORT_CONFIG_VERSION,
    )
