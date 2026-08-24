"""Generic, read-only sampling of one GeoTIFF pixel from a WGS84 location."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
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


def _epsg_code(crs: rasterio.crs.CRS) -> int | None:
    """Use the embedded WKT authority when Rasterio's strict lookup returns None."""

    direct = crs.to_epsg()
    if direct is not None:
        return direct
    return PyprojCRS.from_wkt(crs.to_wkt()).to_epsg(min_confidence=20)


def _grid_signature(dataset: rasterio.io.DatasetReader) -> str:
    epsg = _epsg_code(dataset.crs) if dataset.crs else None
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

    def sample(self, location: Location) -> RasterSample:
        if not self.raster_path.is_file():
            raise FileNotFoundError(f"Raster does not exist: {self.raster_path}")

        # Explicit read-only mode ensures this component never modifies the GeoTIFF.
        with rasterio.open(self.raster_path, mode="r") as dataset:
            if dataset.crs is None:
                raise RasterCrsMissingError(f"Raster has no CRS: {self.raster_path}")
            if self.band > dataset.count:
                raise RasterBandError(
                    f"Raster has {dataset.count} band(s); requested band {self.band}"
                )

            transformer = Transformer.from_crs(WGS84_CRS, dataset.crs, always_xy=True)
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
                source_epsg=_epsg_code(dataset.crs),
                projected_x=float(projected_x),
                projected_y=float(projected_y),
                pixel_row=int(row),
                pixel_col=int(col),
                source_path=str(self.raster_path),
                band=self.band,
                nodata_value=_serialized_nodata(dataset.nodatavals[self.band - 1]),
                grid_signature=_grid_signature(dataset),
            )
