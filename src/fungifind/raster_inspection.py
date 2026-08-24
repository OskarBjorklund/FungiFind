"""Memory-bounded GeoTIFF inspection with streamed or sampled statistics."""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from PIL import Image
from pyproj import CRS as PyprojCRS
from rasterio.enums import Resampling
from rasterio.windows import Window

PERCENTILES = (1, 5, 25, 50, 75, 95, 99)


@dataclass(frozen=True, slots=True)
class InspectionOptions:
    """Hard limits that keep national raster inspection memory-bounded."""

    exact_scan_pixel_limit: int = 10_000_000
    max_sample_pixels: int = 1_000_000
    percentile_sample_size: int = 250_000
    preview_max_dimension: int = 1024
    read_window_size: int = 1024
    valid_value_sample_size: int = 20
    categorical_max_unique_values: int = 256

    def __post_init__(self) -> None:
        for name in (
            "exact_scan_pixel_limit",
            "max_sample_pixels",
            "percentile_sample_size",
            "preview_max_dimension",
            "read_window_size",
            "valid_value_sample_size",
            "categorical_max_unique_values",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1")


class _PriorityReservoir:
    """Uniform bounded sample using random priorities, updated one window at a time."""

    def __init__(self, max_size: int, seed: int) -> None:
        self.max_size = max_size
        self._rng = np.random.default_rng(seed)
        self._values = np.empty(0, dtype=np.float64)
        self._priorities = np.empty(0, dtype=np.float64)

    def add(self, values: np.ndarray) -> None:
        if values.size == 0:
            return
        values = np.asarray(values, dtype=np.float64).reshape(-1)
        priorities = self._rng.random(values.size)
        candidates = np.concatenate((self._values, values))
        candidate_priorities = np.concatenate((self._priorities, priorities))
        if candidates.size > self.max_size:
            selected = np.argpartition(candidate_priorities, -self.max_size)[-self.max_size :]
            candidates = candidates[selected]
            candidate_priorities = candidate_priorities[selected]
        self._values = candidates
        self._priorities = candidate_priorities

    @property
    def values(self) -> np.ndarray:
        return self._values


class _StreamingStatistics:
    """Numerically stable statistics that never retain more than a bounded reservoir."""

    def __init__(
        self,
        reservoir_size: int,
        seed: int,
        *,
        track_integer_classes: bool,
        max_unique_classes: int,
    ) -> None:
        self.examined_count = 0
        self.nodata_count = 0
        self.non_finite_count = 0
        self.valid_count = 0
        self.minimum: float | None = None
        self.maximum: float | None = None
        self.mean = 0.0
        self.m2 = 0.0
        self.reservoir = _PriorityReservoir(reservoir_size, seed)
        self._class_counts: dict[int, int] | None = {} if track_integer_classes else None
        self._class_limit_exceeded = False
        self._max_unique_classes = max_unique_classes

    def update(self, data: np.ma.MaskedArray) -> None:
        values = np.ma.asarray(data)
        mask = np.ma.getmaskarray(values)
        raw = np.asarray(values.data)
        self.examined_count += raw.size
        self.nodata_count += int(np.count_nonzero(mask))

        unmasked = raw[~mask]
        finite = np.isfinite(unmasked)
        self.non_finite_count += int(np.count_nonzero(~finite))
        valid = np.asarray(unmasked[finite], dtype=np.float64)
        if valid.size == 0:
            return

        if self._class_counts is not None:
            class_values, class_counts = np.unique(valid.astype(np.int64), return_counts=True)
            for class_value, class_count in zip(class_values, class_counts, strict=True):
                key = int(class_value)
                self._class_counts[key] = self._class_counts.get(key, 0) + int(class_count)
            if len(self._class_counts) > self._max_unique_classes:
                # Stop retaining class values if this integer raster is actually
                # continuous/high-cardinality. The limit is independent of raster size.
                self._class_counts = None
                self._class_limit_exceeded = True

        batch_count = int(valid.size)
        batch_mean = float(np.mean(valid, dtype=np.float64))
        batch_m2 = float(np.sum((valid - batch_mean) ** 2, dtype=np.float64))
        batch_minimum = float(np.min(valid))
        batch_maximum = float(np.max(valid))

        if self.valid_count == 0:
            self.minimum = batch_minimum
            self.maximum = batch_maximum
            self.mean = batch_mean
            self.m2 = batch_m2
        else:
            combined_count = self.valid_count + batch_count
            delta = batch_mean - self.mean
            self.mean += delta * batch_count / combined_count
            self.m2 += batch_m2 + delta**2 * self.valid_count * batch_count / combined_count
            self.minimum = min(self.minimum, batch_minimum)  # type: ignore[arg-type]
            self.maximum = max(self.maximum, batch_maximum)  # type: ignore[arg-type]
        self.valid_count += batch_count
        self.reservoir.add(valid)

    def result(self, source_fully_scanned: bool, requested_sample_size: int) -> dict[str, Any]:
        examined = self.examined_count
        valid = self.valid_count
        sample = self.reservoir.values
        percentiles = (
            {str(value): float(np.percentile(sample, value)) for value in PERCENTILES}
            if sample.size
            else {str(value): None for value in PERCENTILES}
        )
        denominator = examined or 1
        percentile_is_approximate = sample.size < valid
        if self._class_counts is not None:
            class_distribution: dict[str, Any] | None = {
                "status": "available",
                "unique_valid_values": sorted(self._class_counts),
                "classes": [
                    {
                        "value": value,
                        "sample_count": count,
                        "percentage_of_valid": 100.0 * count / (valid or 1),
                    }
                    for value, count in sorted(self._class_counts.items())
                ],
                "is_approximate": not source_fully_scanned,
                "valid_pixels_counted": valid,
            }
        elif self._class_limit_exceeded:
            class_distribution = {
                "status": "omitted_unique_value_limit_exceeded",
                "max_unique_values": self._max_unique_classes,
                "is_approximate": not source_fully_scanned,
                "valid_pixels_counted": valid,
            }
        else:
            class_distribution = None

        return {
            "minimum": self.minimum,
            "maximum": self.maximum,
            "mean": self.mean if valid else None,
            "standard_deviation": math.sqrt(self.m2 / valid) if valid else None,
            "percentiles": percentiles,
            "percentiles_are_approximate": percentile_is_approximate,
            "nodata_percentage": 100.0 * self.nodata_count / denominator,
            "valid_percentage": 100.0 * valid / denominator,
            "non_finite_percentage": 100.0 * self.non_finite_count / denominator,
            "pixel_percentages_are_approximate": not source_fully_scanned,
            "examined_pixel_count": examined,
            "valid_examined_pixel_count": valid,
            "nodata_examined_pixel_count": self.nodata_count,
            "sample_of_valid_values": [float(value) for value in sample[:requested_sample_size]],
            "requested_valid_value_sample_size": requested_sample_size,
            "class_distribution": class_distribution,
        }


def discover_rasters(input_path: str | Path) -> list[Path]:
    """Resolve one GeoTIFF or the immediate GeoTIFF children of a directory."""

    path = Path(input_path)
    if path.is_file():
        if path.suffix.lower() not in {".tif", ".tiff"}:
            raise ValueError(f"Expected a .tif or .tiff file, got: {path}")
        return [path.resolve()]
    if path.is_dir():
        rasters = sorted(
            candidate.resolve()
            for candidate in path.iterdir()
            if candidate.is_file() and candidate.suffix.lower() in {".tif", ".tiff"}
        )
        if not rasters:
            raise ValueError(f"No .tif or .tiff files found in: {path}")
        return rasters
    raise FileNotFoundError(f"Raster input does not exist: {path}")


def _window_grid(width: int, height: int, window_size: int) -> Iterator[Window]:
    for row_offset in range(0, height, window_size):
        window_height = min(window_size, height - row_offset)
        for column_offset in range(0, width, window_size):
            window_width = min(window_size, width - column_offset)
            yield Window(column_offset, row_offset, window_width, window_height)


def _bounded_shape(width: int, height: int, max_pixels: int) -> tuple[int, int]:
    if width * height <= max_pixels:
        return height, width
    scale = math.sqrt(max_pixels / (width * height))
    sampled_width = max(1, int(width * scale))
    sampled_height = max(1, int(height * scale))
    while sampled_width * sampled_height > max_pixels:
        if sampled_width >= sampled_height:
            sampled_width -= 1
        else:
            sampled_height -= 1
    return sampled_height, sampled_width


def _bounded_preview_shape(width: int, height: int, max_dimension: int) -> tuple[int, int]:
    if max(width, height) <= max_dimension:
        return height, width
    scale = max_dimension / max(width, height)
    return max(1, int(height * scale)), max(1, int(width * scale))


def _band_statistics(
    dataset: rasterio.io.DatasetReader,
    band_index: int,
    options: InspectionOptions,
    source_fully_scanned: bool,
) -> tuple[dict[str, Any], tuple[int, int] | None]:
    statistics = _StreamingStatistics(
        reservoir_size=options.percentile_sample_size,
        seed=20260823 + band_index,
        track_integer_classes=np.issubdtype(np.dtype(dataset.dtypes[band_index - 1]), np.integer),
        max_unique_classes=options.categorical_max_unique_values,
    )
    sample_shape: tuple[int, int] | None = None
    if source_fully_scanned:
        for window in _window_grid(dataset.width, dataset.height, options.read_window_size):
            statistics.update(dataset.read(band_index, window=window, masked=True))
    else:
        sample_shape = _bounded_shape(dataset.width, dataset.height, options.max_sample_pixels)
        sampled = dataset.read(
            band_index,
            out_shape=sample_shape,
            masked=True,
            resampling=Resampling.nearest,
        )
        statistics.update(sampled)
    return statistics.result(source_fully_scanned, options.valid_value_sample_size), sample_shape


def _tag_metadata(dataset: rasterio.io.DatasetReader, band_index: int | None = None) -> dict[str, Any]:
    if band_index is None:
        namespaces = set(dataset.tag_namespaces())
    else:
        namespaces = set(dataset.tag_namespaces(band_index))
    namespaces.add(None)
    tags: dict[str, Any] = {}
    for namespace in sorted(namespaces, key=lambda value: "" if value is None else value):
        if band_index is None:
            namespace_tags = dataset.tags(ns=namespace)
        else:
            namespace_tags = dataset.tags(band_index, ns=namespace)
        if namespace_tags:
            tags[namespace or "default"] = namespace_tags
    return tags


def _json_scalar(value: Any) -> Any:
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            return "NaN"
        return "Infinity" if value > 0 else "-Infinity"
    return value


def _compression_name(dataset: rasterio.io.DatasetReader) -> str | None:
    compression = dataset.compression
    if compression is None:
        return None
    return str(getattr(compression, "value", compression))


def _epsg_code(crs: rasterio.crs.CRS | None) -> int | None:
    """Resolve embedded authorities even when Rasterio's strict match returns None."""

    if crs is None:
        return None
    direct = crs.to_epsg()
    if direct is not None:
        return direct
    return PyprojCRS.from_wkt(crs.to_wkt()).to_epsg(min_confidence=20)


def _human_file_size(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.2f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def _create_preview(
    dataset: rasterio.io.DatasetReader,
    output_path: Path,
    options: InspectionOptions,
) -> dict[str, Any]:
    preview_height, preview_width = _bounded_preview_shape(
        dataset.width, dataset.height, options.preview_max_dimension
    )
    preview = dataset.read(
        1,
        out_shape=(preview_height, preview_width),
        masked=True,
        resampling=Resampling.nearest,
    )
    raw = np.asarray(preview.data)
    valid_mask = ~np.ma.getmaskarray(preview) & np.isfinite(raw)
    rgba = np.zeros((preview_height, preview_width, 4), dtype=np.uint8)
    display_minimum: float | None = None
    display_maximum: float | None = None
    if np.any(valid_mask):
        valid = np.asarray(raw[valid_mask], dtype=np.float64)
        display_minimum = float(np.min(valid))
        display_maximum = float(np.max(valid))
        if display_minimum == display_maximum:
            gray = np.full(raw.shape, 127, dtype=np.uint8)
        else:
            scaled = (np.asarray(raw, dtype=np.float64) - display_minimum) / (
                display_maximum - display_minimum
            )
            gray = np.asarray(np.clip(scaled, 0.0, 1.0) * 255.0, dtype=np.uint8)
        rgba[..., 0] = gray
        rgba[..., 1] = gray
        rgba[..., 2] = gray
        rgba[..., 3] = np.where(valid_mask, 255, 0).astype(np.uint8)
    Image.fromarray(rgba, mode="RGBA").save(output_path, format="PNG", optimize=True)
    return {
        "path": str(output_path.resolve()),
        "source_band": 1,
        "width": preview_width,
        "height": preview_height,
        "resampling": "nearest",
        "display_scaling": "linear_min_max_of_preview_sample",
        "display_minimum": display_minimum,
        "display_maximum": display_maximum,
        "note": (
            "Visualization only. Display scaling affects the PNG, not the source raster; "
            "no semantic interpretation was applied."
        ),
    }


def inspect_raster(
    raster_path: str | Path,
    output_directory: str | Path = "reports/raster_inspection",
    options: InspectionOptions | None = None,
) -> dict[str, Any]:
    """Inspect one raster, write JSON and PNG, and return the JSON-compatible report."""

    options = options or InspectionOptions()
    path = Path(raster_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Raster does not exist: {path}")
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    json_path = output / f"{path.stem}.json"
    preview_path = output / f"{path.stem}_preview.png"
    size_bytes = path.stat().st_size

    with rasterio.open(path) as dataset:
        source_pixels_per_band = dataset.width * dataset.height
        source_pixels_total = source_pixels_per_band * dataset.count
        source_fully_scanned = source_pixels_total <= options.exact_scan_pixel_limit
        band_statistics = []
        sampled_shape: tuple[int, int] | None = None
        for band_index in dataset.indexes:
            statistics, band_sample_shape = _band_statistics(
                dataset, band_index, options, source_fully_scanned
            )
            sampled_shape = sampled_shape or band_sample_shape
            band_statistics.append({"band": band_index, **statistics})

        overview_levels = {
            str(band_index): dataset.overviews(band_index) for band_index in dataset.indexes
        }
        approximate_fields: list[str] = []
        if not source_fully_scanned:
            approximate_fields.append("all_statistics")
        elif any(item["percentiles_are_approximate"] for item in band_statistics):
            approximate_fields.append("percentiles")

        crs = dataset.crs
        report: dict[str, Any] = {
            "filename": path.name,
            "path": str(path),
            "file_size_bytes": size_bytes,
            "file_size_human": _human_file_size(size_bytes),
            "driver": dataset.driver,
            "crs": crs.to_string() if crs else None,
            "epsg": _epsg_code(crs),
            "width": dataset.width,
            "height": dataset.height,
            "band_count": dataset.count,
            "pixel_resolution": {"x": dataset.res[0], "y": dataset.res[1]},
            "bounds": {
                "left": dataset.bounds.left,
                "bottom": dataset.bounds.bottom,
                "right": dataset.bounds.right,
                "top": dataset.bounds.top,
            },
            "transform": {
                "a": dataset.transform.a,
                "b": dataset.transform.b,
                "c": dataset.transform.c,
                "d": dataset.transform.d,
                "e": dataset.transform.e,
                "f": dataset.transform.f,
                "gdal": list(dataset.transform.to_gdal()),
            },
            "dtypes": list(dataset.dtypes),
            "nodata_value": _json_scalar(dataset.nodata),
            "nodata_values_by_band": [_json_scalar(value) for value in dataset.nodatavals],
            "band_descriptions": list(dataset.descriptions),
            "units": list(dataset.units),
            "tags": _tag_metadata(dataset),
            "band_tags": {
                str(band_index): _tag_metadata(dataset, band_index)
                for band_index in dataset.indexes
            },
            "compression": _compression_name(dataset),
            "is_tiled": bool(dataset.profile.get("tiled", False)),
            "block_sizes": [
                {"band": band_index, "height": shape[0], "width": shape[1]}
                for band_index, shape in zip(dataset.indexes, dataset.block_shapes, strict=True)
            ],
            "overview_levels": overview_levels,
            "has_overviews": any(overview_levels.values()),
            "statistics": {
                "mode": "exact_streamed" if source_fully_scanned else "approximate_downsampled",
                "are_approximate": bool(approximate_fields),
                "approximate_fields": approximate_fields,
                "source_pixels_per_band": source_pixels_per_band,
                "source_pixels_total": source_pixels_total,
                "sample_shape_per_band": (
                    {"height": sampled_shape[0], "width": sampled_shape[1]}
                    if sampled_shape
                    else None
                ),
                "sampling_resampling": None if source_fully_scanned else "nearest",
                "memory_safety": (
                    f"Reads at most {options.read_window_size}x{options.read_window_size} "
                    "source pixels at once during an exact scan; large rasters use a "
                    f"maximum {options.max_sample_pixels}-pixel grid per band."
                ),
                "bands": band_statistics,
            },
        }
        report["preview"] = _create_preview(dataset, preview_path, options)

    report["json_report_path"] = str(json_path.resolve())
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
    return report


def format_terminal_report(report: dict[str, Any]) -> str:
    """Format a concise, human-readable summary without hiding approximation status."""

    statistics = report["statistics"]
    resolution = report["pixel_resolution"]
    lines = [
        f"\n{report['filename']}",
        f"  Size: {report['file_size_human']} ({report['file_size_bytes']} bytes)",
        f"  CRS: {report['crs']} | EPSG: {report['epsg']}",
        (
            f"  Dimensions: {report['width']} x {report['height']} | "
            f"Bands: {report['band_count']} | Dtypes: {', '.join(report['dtypes'])}"
        ),
        f"  Resolution: {resolution['x']} x {resolution['y']}",
        f"  Bounds: {report['bounds']}",
        f"  NoData: {report['nodata_values_by_band']}",
        (
            f"  Storage: compression={report['compression']}, tiled={report['is_tiled']}, "
            f"blocks={report['block_sizes']}"
        ),
        f"  Overviews: {report['overview_levels']}",
        (
            f"  Statistics: {statistics['mode']} | approximate="
            f"{statistics['are_approximate']} | fields={statistics['approximate_fields']}"
        ),
    ]
    for band in statistics["bands"]:
        percentiles = band["percentiles"]
        lines.append(
            "  Band {band}: min={minimum}, max={maximum}, mean={mean}, std={std}, "
            "p1/p50/p99={p1}/{p50}/{p99}, NoData={nodata:.3f}%, valid={valid:.3f}%".format(
                band=band["band"],
                minimum=band["minimum"],
                maximum=band["maximum"],
                mean=band["mean"],
                std=band["standard_deviation"],
                p1=percentiles["1"],
                p50=percentiles["50"],
                p99=percentiles["99"],
                nodata=band["nodata_percentage"],
                valid=band["valid_percentage"],
            )
        )
        distribution = band.get("class_distribution")
        if distribution is not None:
            if distribution["status"] == "available":
                class_summary = ", ".join(
                    f"{item['value']}={item['percentage_of_valid']:.2f}%"
                    for item in distribution["classes"]
                )
                lines.append(
                    "    Classes: "
                    f"{class_summary} | approximate={distribution['is_approximate']}"
                )
            else:
                lines.append(f"    Classes: {distribution['status']}")
    lines.extend(
        [
            f"  JSON: {report['json_report_path']}",
            f"  Preview: {report['preview']['path']}",
        ]
    )
    return "\n".join(lines)
