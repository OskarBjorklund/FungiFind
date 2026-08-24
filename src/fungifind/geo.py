"""Coordinate transforms kept outside the scoring domain."""

from __future__ import annotations

from dataclasses import dataclass

from fungifind.models import Location

WGS84_CRS = "EPSG:4326"


@dataclass(frozen=True, slots=True)
class ProjectedCoordinate:
    x: float
    y: float
    crs: str


def project_location(location: Location, target_crs: str) -> ProjectedCoordinate:
    """Project WGS84 input to any adapter-selected CRS, for example a raster's CRS."""

    try:
        from pyproj import Transformer
    except ImportError as exc:  # pragma: no cover - depends on optional installation
        raise RuntimeError("CRS transformation requires: pip install '.[geo]'") from exc

    transformer = Transformer.from_crs(WGS84_CRS, target_crs, always_xy=True)
    x, y = transformer.transform(location.longitude, location.latitude)
    return ProjectedCoordinate(x=x, y=y, crs=target_crs)

