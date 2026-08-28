"""Generic, read-only sampling of one GeoTIFF pixel from a WGS84 location."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import RLock
from typing import Any

import numpy as np
import rasterio
from pyproj import CRS as PyprojCRS
from pyproj import Transformer
from rasterio.windows import Window

from fungifind.models import Location

WGS84_CRS = "EPSG:4326"


class RasterPointError(RuntimeError):
    """Base error for explicit raster point-sampling failures."""


class RasterCrsMissingError(RasterPointError):
    """Raised when a raster cannot transform WGS84 input because it has no CRS."""


class RasterPointOutsideBoundsError(RasterPointError):
    """Raised when the transformed point does not map to a source pixel."""


class RasterBandError(RasterPointError):
    """Raised when a requested band does not exist."""


@dataclass(frozen=True, slots=True)
class RasterSample:
    """One raw raster cell plus enough context to audit how it was selected."""

    value: float | int | None
    raw_value: float | int | None
    is_nodata: bool
    source_crs: str
    source_epsg: int | None
    projected_x: float
    projected_y: float
    pixel_row: int
    pixel_col: int
    source_path: str
    band: int
    nodata_value: float | int | str | None
    grid_signature: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _python_scalar(value: Any) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, (int, float)):
        return value
    raise RasterPointError(f"Unsupported raster scalar type: {type(value).__name__}")


def _serialized_nodata(value: Any) -> float | int | str | None:
    scalar = _python_scalar(value)
    if isinstance(scalar, float) and not math.isfinite(scalar):
        return "NaN" if math.isnan(scalar) else ("Infinity" if scalar > 0 else "-Infinity")
    return scalar


def raster_epsg_code(crs: rasterio.crs.CRS) -> int | None:
    """Use the embedded WKT authority when Rasterio's strict lookup returns None."""

    direct = crs.to_epsg()
    if direct is not None:
        return direct
    return PyprojCRS.from_wkt(crs.to_wkt()).to_epsg(min_confidence=20)


def horizontal_crs(crs: rasterio.crs.CRS | PyprojCRS | str) -> PyprojCRS:
    """Return the horizontal part used for 2D WGS84-to-grid transformations."""

    parsed = PyprojCRS.from_user_input(crs)
    if not parsed.is_compound:
        return parsed
    horizontal_parts = [
        part for part in parsed.sub_crs_list if part.is_projected or part.is_geographic
    ]
    if len(horizontal_parts) != 1:
        raise RasterPointError(
            f"Compound CRS {parsed.name!r} has {len(horizontal_parts)} horizontal parts"
        )
    return horizontal_parts[0]


def raster_grid_signature(dataset: rasterio.io.DatasetReader) -> str:
    epsg = raster_epsg_code(dataset.crs) if dataset.crs else None
    payload = {
        "crs": f"EPSG:{epsg}" if epsg is not None else dataset.crs.to_wkt(),
        # GeoTIFF WKT/affine serialization can contain sub-nanometre floating
        # noise. Nine decimals preserves meaningful grid differences while
        # treating 199999.99999999994 and 200000.0 as the same grid origin.
        "transform_gdal": [round(value, 9) for value in dataset.transform.to_gdal()],
        "width": dataset.width,
        "height": dataset.height,
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class RasterPointReader:
    """Transform WGS84 and read exactly one requested 1×1 raster window."""

    def __init__(self, raster_path: str | Path, band: int = 1) -> None:
        self.raster_path = Path(raster_path).resolve()
        self.band = band
        if band < 1:
            raise RasterBandError("band must be at least 1")
        self._lock = RLock()
        self._dataset: rasterio.io.DatasetReader | None = None
        self._transformer: Transformer | None = None

    def _open_dataset(self) -> tuple[rasterio.io.DatasetReader, Transformer]:
        dataset = self._dataset
        if dataset is None or dataset.closed:
            if not self.raster_path.is_file():
                raise FileNotFoundError(f"Raster does not exist: {self.raster_path}")
            dataset = rasterio.open(self.raster_path, mode="r")
            if dataset.crs is None:
                dataset.close()
                raise RasterCrsMissingError(f"Raster has no CRS: {self.raster_path}")
            if self.band > dataset.count:
                count = dataset.count
                dataset.close()
                raise RasterBandError(
                    f"Raster has {count} band(s); requested band {self.band}"
                )
            self._dataset = dataset
            self._transformer = Transformer.from_crs(
                WGS84_CRS,
                horizontal_crs(dataset.crs),
                always_xy=True,
            )
        if self._transformer is None:  # defensive guard for static type checking
            raise RasterPointError("Raster transformer was not initialized")
        return dataset, self._transformer

    def close(self) -> None:
        """Close the reusable read-only dataset handle."""

        with self._lock:
            if self._dataset is not None:
                self._dataset.close()
            self._dataset = None
            self._transformer = None

    def sample(self, location: Location) -> RasterSample:
        # Rasterio dataset readers are not assumed to be thread-safe. The lock
        # lets viewport requests reuse one read-only handle without overlapping
        # window reads from concurrent FastAPI worker threads.
        with self._lock:
            dataset, transformer = self._open_dataset()
            projected_x, projected_y = transformer.transform(
                location.longitude,
                location.latitude,
            )
            if not math.isfinite(projected_x) or not math.isfinite(projected_y):
                raise RasterPointOutsideBoundsError(
                    f"Coordinate could not be projected into {dataset.crs.to_string()}"
                )

            row, col = dataset.index(projected_x, projected_y)
            if row < 0 or row >= dataset.height or col < 0 or col >= dataset.width:
                raise RasterPointOutsideBoundsError(
                    "WGS84 coordinate "
                    f"({location.latitude}, {location.longitude}) projects to "
                    f"({projected_x}, {projected_y}) in {dataset.crs.to_string()}, "
                    f"outside raster bounds {dataset.bounds}"
                )

            pixel = dataset.read(
                self.band,
                window=Window(col_off=col, row_off=row, width=1, height=1),
                masked=True,
            )
            if pixel.shape != (1, 1):
                raise RasterPointError(
                    f"Expected one raster cell, received array with shape {pixel.shape}"
                )
            raw_value = _python_scalar(pixel.data[0, 0])
            mask = np.ma.getmaskarray(pixel)
            is_nodata = bool(mask[0, 0])
            if isinstance(raw_value, float) and not math.isfinite(raw_value):
                is_nodata = True

            crs_text = dataset.crs.to_string()
            return RasterSample(
                value=None if is_nodata else raw_value,
                raw_value=raw_value,
                is_nodata=is_nodata,
                source_crs=crs_text,
                source_epsg=raster_epsg_code(dataset.crs),
                projected_x=float(projected_x),
                projected_y=float(projected_y),
                pixel_row=int(row),
                pixel_col=int(col),
                source_path=str(self.raster_path),
                band=self.band,
                nodata_value=_serialized_nodata(dataset.nodatavals[self.band - 1]),
                grid_signature=raster_grid_signature(dataset),
            )

    def sample_many(self, locations: Sequence[Location]) -> tuple[RasterSample, ...]:
        """Sample many points by reading every touched raster block only once."""

        if not locations:
            return ()
        with self._lock:
            dataset, transformer = self._open_dataset()
            projected_xs, projected_ys = transformer.transform(
                [location.longitude for location in locations],
                [location.latitude for location in locations],
            )
            rows_and_columns: list[tuple[int, int]] = []
            for location, projected_x, projected_y in zip(
                locations, projected_xs, projected_ys, strict=True
            ):
                if not math.isfinite(projected_x) or not math.isfinite(projected_y):
                    raise RasterPointOutsideBoundsError(
                        f"Coordinate could not be projected into {dataset.crs.to_string()}"
                    )
                row, col = dataset.index(projected_x, projected_y)
                if row < 0 or row >= dataset.height or col < 0 or col >= dataset.width:
                    raise RasterPointOutsideBoundsError(
                        "WGS84 coordinate "
                        f"({location.latitude}, {location.longitude}) projects outside "
                        f"raster bounds {dataset.bounds}"
                    )
                rows_and_columns.append((int(row), int(col)))

            block_height, block_width = dataset.block_shapes[self.band - 1]
            grouped: dict[tuple[int, int], list[int]] = defaultdict(list)
            for index, (row, col) in enumerate(rows_and_columns):
                grouped[(row // block_height, col // block_width)].append(index)

            raw_values: list[float | int | None] = [None] * len(locations)
            nodata_flags = [False] * len(locations)
            for (block_row, block_col), indices in grouped.items():
                row_offset = block_row * block_height
                col_offset = block_col * block_width
                height = min(block_height, dataset.height - row_offset)
                width = min(block_width, dataset.width - col_offset)
                block = dataset.read(
                    self.band,
                    window=Window(col_offset, row_offset, width, height),
                    masked=True,
                )
                mask = np.ma.getmaskarray(block)
                for index in indices:
                    row, col = rows_and_columns[index]
                    local_row = row - row_offset
                    local_col = col - col_offset
                    raw = _python_scalar(block.data[local_row, local_col])
                    is_nodata = bool(mask[local_row, local_col])
                    if isinstance(raw, float) and not math.isfinite(raw):
                        is_nodata = True
                    raw_values[index] = raw
                    nodata_flags[index] = is_nodata

            crs_text = dataset.crs.to_string()
            epsg = raster_epsg_code(dataset.crs)
            nodata_value = _serialized_nodata(dataset.nodatavals[self.band - 1])
            signature = raster_grid_signature(dataset)
            return tuple(
                RasterSample(
                    value=None if nodata_flags[index] else raw_values[index],
                    raw_value=raw_values[index],
                    is_nodata=nodata_flags[index],
                    source_crs=crs_text,
                    source_epsg=epsg,
                    projected_x=float(projected_xs[index]),
                    projected_y=float(projected_ys[index]),
                    pixel_row=rows_and_columns[index][0],
                    pixel_col=rows_and_columns[index][1],
                    source_path=str(self.raster_path),
                    band=self.band,
                    nodata_value=nodata_value,
                    grid_signature=signature,
                )
                for index in range(len(locations))
            )
