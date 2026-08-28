"""Manifest-indexed elevation, slope, and aspect sampling from local DEM tiles."""

from __future__ import annotations

import json
import math
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any

import numpy as np
import rasterio
from pyproj import CRS as PyprojCRS
from pyproj import Transformer
from rasterio.windows import Window

from fungifind.data_sources.raster import (
    WGS84_CRS,
    RasterCrsMissingError,
    RasterPointError,
    horizontal_crs,
    raster_epsg_code,
    raster_grid_signature,
)
from fungifind.models import (
    DataSourceMetadata,
    FeatureProvenance,
    FeatureSnapshot,
    Location,
    StaticHabitatFeatures,
)

HORN_METHOD = "Horn 3x3 weighted finite-difference gradient"
ASPECT_CONVENTION = "0=north,90=east,180=south,270=west; flat=None"
NEIGHBORHOOD_SIZE = 3


class ElevationManifestError(RasterPointError):
    """Raised when the local elevation manifest is invalid or incomplete."""


class ElevationTileNotFoundError(RasterPointError):
    """Raised when no downloaded DEM tile covers a WGS84 point."""


@dataclass(frozen=True, slots=True)
class ElevationTile:
    item_id: str
    asset_key: str
    source_href: str
    source_path: Path
    source_filename: str
    item_bbox_wgs84: tuple[float, float, float, float]
    manifest_proj_epsg: int | None
    manifest_proj_bbox: tuple[float, ...] | None

    def bbox_contains(self, location: Location) -> bool:
        left, bottom, right, top = self.item_bbox_wgs84
        return (
            left <= location.longitude <= right
            and bottom <= location.latitude <= top
        )


@dataclass(frozen=True, slots=True)
class ElevationTileMatch:
    tile: ElevationTile
    projected_x: float
    projected_y: float
    pixel_row: int
    pixel_col: int
    source_crs: str
    source_epsg: int | None
    horizontal_crs: str
    horizontal_epsg: int | None
    vertical_crs: str | None
    vertical_epsg: int | None
    has_full_neighborhood: bool


@dataclass(frozen=True, slots=True)
class TerrainSampleResult:
    snapshot: FeatureSnapshot[StaticHabitatFeatures]
    tile_match: ElevationTileMatch
    method: str = HORN_METHOD
    neighborhood_size: int = NEIGHBORHOOD_SIZE


def _number_tuple(value: object, *, expected_length: int | None = None) -> tuple[float, ...]:
    if not isinstance(value, list):
        raise ElevationManifestError("Expected a numeric list in elevation manifest")
    try:
        result = tuple(float(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise ElevationManifestError("Elevation manifest contains non-numeric bounds") from exc
    if expected_length is not None and len(result) != expected_length:
        raise ElevationManifestError(
            f"Expected {expected_length} bounds values, received {len(result)}"
        )
    if not all(math.isfinite(item) for item in result):
        raise ElevationManifestError("Elevation manifest bounds must be finite")
    return result


def _optional_epsg(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _safe_local_path(directory: Path, filename: object) -> Path:
    if not isinstance(filename, str) or not filename.strip():
        raise ElevationManifestError("Elevation asset has no local filename")
    path = (directory / filename).resolve()
    if directory != path.parent and directory not in path.parents:
        raise ElevationManifestError(f"Elevation filename escapes its data directory: {filename}")
    return path


def _crs_parts(
    crs: rasterio.crs.CRS,
) -> tuple[PyprojCRS, PyprojCRS | None]:
    parsed = PyprojCRS.from_user_input(crs)
    horizontal = horizontal_crs(parsed)
    vertical = next(
        (part for part in parsed.sub_crs_list if part.is_vertical),
        None,
    )
    return horizontal, vertical


class ElevationTileIndex:
    """Find a locally downloaded DEM tile using manifest WGS84 bounds."""

    def __init__(self, manifest_path: str | Path = "src/data/elevation/manifest.json") -> None:
        self.manifest_path = Path(manifest_path).resolve()
        if not self.manifest_path.is_file():
            raise FileNotFoundError(f"Elevation manifest does not exist: {self.manifest_path}")
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ElevationManifestError(
                f"Invalid elevation manifest JSON: {self.manifest_path}"
            ) from exc
        assets = manifest.get("assets") if isinstance(manifest, dict) else None
        if not isinstance(assets, list):
            raise ElevationManifestError("Elevation manifest has no assets list")

        directory = self.manifest_path.parent.resolve()
        tiles: list[ElevationTile] = []
        seen_paths: set[Path] = set()
        for record in assets:
            if not isinstance(record, dict):
                continue
            media_type = str(record.get("media_type", "")).casefold()
            roles = {str(role).casefold() for role in record.get("roles", [])}
            filename = record.get("local_filename")
            if "geotiff" not in media_type and not str(filename).casefold().endswith(
                (".tif", ".tiff")
            ):
                continue
            if roles and "data" not in roles:
                continue
            source_path = _safe_local_path(directory, filename)
            if source_path in seen_paths:
                continue
            if not source_path.is_file():
                raise ElevationManifestError(
                    f"Manifest DEM file does not exist: {source_path}"
                )
            seen_paths.add(source_path)
            bbox = _number_tuple(record.get("item_bbox_wgs84"), expected_length=4)
            raw_proj_bbox = record.get("proj:bbox")
            proj_bbox = (
                _number_tuple(raw_proj_bbox) if raw_proj_bbox is not None else None
            )
            item_id = str(record.get("item_id", ""))
            asset_key = str(record.get("asset_key", ""))
            source_href = str(record.get("source_href", ""))
            if not item_id or not asset_key or not source_href:
                raise ElevationManifestError("Elevation asset lacks item/asset/source identity")
            tiles.append(
                ElevationTile(
                    item_id=item_id,
                    asset_key=asset_key,
                    source_href=source_href,
                    source_path=source_path,
                    source_filename=source_path.name,
                    item_bbox_wgs84=(bbox[0], bbox[1], bbox[2], bbox[3]),
                    manifest_proj_epsg=_optional_epsg(record.get("proj:epsg")),
                    manifest_proj_bbox=proj_bbox,
                )
            )
        if not tiles:
            raise ElevationManifestError("Elevation manifest contains no local DEM tiles")
        self.tiles = tuple(sorted(tiles, key=lambda tile: (tile.item_id, tile.source_filename)))
        self._resource_lock = RLock()
        self._datasets: dict[Path, rasterio.io.DatasetReader] = {}
        self._transformers: dict[Path, Transformer] = {}
        self._crs_parts_cache: dict[Path, tuple[PyprojCRS, PyprojCRS | None]] = {}

    def _resources(
        self, tile: ElevationTile
    ) -> tuple[
        rasterio.io.DatasetReader,
        Transformer,
        PyprojCRS,
        PyprojCRS | None,
    ]:
        dataset = self._datasets.get(tile.source_path)
        if dataset is None or dataset.closed:
            dataset = rasterio.open(tile.source_path, mode="r")
            if dataset.crs is None:
                dataset.close()
                raise RasterCrsMissingError(f"DEM has no CRS: {tile.source_path}")
            horizontal, vertical = _crs_parts(dataset.crs)
            self._datasets[tile.source_path] = dataset
            self._transformers[tile.source_path] = Transformer.from_crs(
                WGS84_CRS, horizontal, always_xy=True
            )
            self._crs_parts_cache[tile.source_path] = (horizontal, vertical)
        transformer = self._transformers[tile.source_path]
        horizontal, vertical = self._crs_parts_cache[tile.source_path]
        return dataset, transformer, horizontal, vertical

    @contextmanager
    def read_resources(self, tile: ElevationTile):
        """Hold the shared read lock while using one cached DEM dataset."""

        with self._resource_lock:
            yield self._resources(tile)

    def close(self) -> None:
        with self._resource_lock:
            for dataset in self._datasets.values():
                dataset.close()
            self._datasets.clear()
            self._transformers.clear()
            self._crs_parts_cache.clear()

    def candidate_tiles(self, location: Location) -> tuple[ElevationTile, ...]:
        return tuple(tile for tile in self.tiles if tile.bbox_contains(location))

    def find_tile(
        self,
        location: Location,
        *,
        neighborhood_radius: int = 1,
    ) -> ElevationTileMatch:
        candidates = self.candidate_tiles(location)
        if not candidates:
            raise ElevationTileNotFoundError(
                f"No downloaded elevation tile bbox covers ({location.latitude}, "
                f"{location.longitude})"
            )
        matches: list[ElevationTileMatch] = []
        for tile in candidates:
            with self.read_resources(tile) as resources:
                dataset, transformer, horizontal, vertical = resources
                projected_x, projected_y = transformer.transform(
                    location.longitude,
                    location.latitude,
                )
                if not math.isfinite(projected_x) or not math.isfinite(projected_y):
                    continue
                row, col = dataset.index(projected_x, projected_y)
                if row < 0 or row >= dataset.height or col < 0 or col >= dataset.width:
                    continue
                margin = neighborhood_radius
                has_full_neighborhood = (
                    row >= margin
                    and col >= margin
                    and row < dataset.height - margin
                    and col < dataset.width - margin
                )
                matches.append(
                    ElevationTileMatch(
                        tile=tile,
                        projected_x=float(projected_x),
                        projected_y=float(projected_y),
                        pixel_row=int(row),
                        pixel_col=int(col),
                        source_crs=dataset.crs.to_string(),
                        source_epsg=raster_epsg_code(dataset.crs),
                        horizontal_crs=horizontal.name,
                        horizontal_epsg=horizontal.to_epsg(min_confidence=20),
                        vertical_crs=vertical.name if vertical is not None else None,
                        vertical_epsg=(
                            vertical.to_epsg(min_confidence=20)
                            if vertical is not None
                            else None
                        ),
                        has_full_neighborhood=has_full_neighborhood,
                    )
                )
        if not matches:
            raise ElevationTileNotFoundError(
                f"Manifest candidates do not actually cover ({location.latitude}, "
                f"{location.longitude}) in their raster grids"
            )
        return max(matches, key=lambda match: match.has_full_neighborhood)


def horn_slope_aspect(
    elevations: np.ndarray,
    *,
    pixel_size_x: float,
    pixel_size_y: float,
) -> tuple[float, float | None]:
    """Calculate Horn 3×3 slope and downslope aspect in degrees.

    Rows run north-to-south and columns west-to-east. Aspect is clockwise from
    north: 0° north, 90° east, 180° south, 270° west. Flat cells return None.
    """

    values = np.asarray(elevations, dtype=np.float64)
    if values.shape != (3, 3):
        raise ValueError(f"Horn requires a 3x3 array, got {values.shape}")
    if not np.all(np.isfinite(values)):
        raise ValueError("Horn elevations must all be finite")
    if pixel_size_x <= 0 or pixel_size_y <= 0:
        raise ValueError("Pixel sizes must be positive")

    z1, z2, z3 = values[0]
    z4, _, z6 = values[1]
    z7, z8, z9 = values[2]
    dz_east = ((z3 + 2 * z6 + z9) - (z1 + 2 * z4 + z7)) / (
        8 * pixel_size_x
    )
    dz_north = ((z1 + 2 * z2 + z3) - (z7 + 2 * z8 + z9)) / (
        8 * pixel_size_y
    )
    gradient = math.hypot(dz_east, dz_north)
    slope = math.degrees(math.atan(gradient))
    if gradient <= 1e-12:
        return 0.0, None
    aspect = math.degrees(math.atan2(-dz_east, -dz_north)) % 360.0
    return slope, aspect


def _python_number(value: Any) -> float | int | None:
    if isinstance(value, np.generic):
        value = value.item()
    return value if isinstance(value, (int, float)) else None


def _nodata_value(dataset: rasterio.io.DatasetReader, band: int) -> float | int | str | None:
    value = _python_number(dataset.nodatavals[band - 1])
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if math.isnan(value) else "Infinity"
    return value


class TerrainDemReader:
    """Read and derive terrain features without exposing them to scoring yet."""

    def __init__(self, tile_index: ElevationTileIndex, *, band: int = 1) -> None:
        if band < 1:
            raise ValueError("band must be at least 1")
        self.tile_index = tile_index
        self.band = band

    @classmethod
    def from_manifest(
        cls,
        manifest_path: str | Path = "src/data/elevation/manifest.json",
    ) -> TerrainDemReader:
        return cls(ElevationTileIndex(manifest_path))

    def sample_terrain(self, location: Location) -> TerrainSampleResult:
        match = self.tile_index.find_tile(location, neighborhood_radius=1)
        tile = match.tile
        with self.tile_index.read_resources(tile) as resources:
            dataset, _, _, _ = resources
            if self.band > dataset.count:
                raise RasterPointError(
                    f"DEM has {dataset.count} band(s); requested band {self.band}"
                )
            if not math.isclose(dataset.transform.b, 0.0, abs_tol=1e-12) or not math.isclose(
                dataset.transform.d, 0.0, abs_tol=1e-12
            ):
                raise RasterPointError("Horn terrain derivation requires a north-up DEM grid")
            pixel_size_x = abs(float(dataset.transform.a))
            pixel_size_y = abs(float(dataset.transform.e))
            row, col = match.pixel_row, match.pixel_col
            if match.has_full_neighborhood:
                window = Window(col - 1, row - 1, NEIGHBORHOOD_SIZE, NEIGHBORHOOD_SIZE)
                neighborhood = dataset.read(self.band, window=window, masked=True)
                raw_center = _python_number(neighborhood.data[1, 1])
                mask = np.ma.getmaskarray(neighborhood)
                center_nodata = bool(mask[1, 1])
            else:
                window = Window(col, row, 1, 1)
                center = dataset.read(self.band, window=window, masked=True)
                raw_center = _python_number(center.data[0, 0])
                center_nodata = bool(np.ma.getmaskarray(center)[0, 0])
                neighborhood = None
                mask = None
            if isinstance(raw_center, float) and not math.isfinite(raw_center):
                center_nodata = True

            elevation = None if center_nodata else float(raw_center)
            slope: float | None = None
            aspect: float | None = None
            if center_nodata:
                derived_status = "central_pixel_nodata"
                aspect_status = derived_status
            elif not match.has_full_neighborhood:
                derived_status = "insufficient_neighborhood_at_tile_edge"
                aspect_status = derived_status
            elif mask is not None and bool(mask.any()):
                derived_status = "neighborhood_contains_nodata"
                aspect_status = derived_status
            elif neighborhood is not None and not np.all(np.isfinite(neighborhood.data)):
                derived_status = "neighborhood_contains_non_finite_value"
                aspect_status = derived_status
            else:
                slope, aspect = horn_slope_aspect(
                    np.asarray(neighborhood.data),
                    pixel_size_x=pixel_size_x,
                    pixel_size_y=pixel_size_y,
                )
                derived_status = "derived_horn_3x3_validated_not_scoring_enabled"
                aspect_status = (
                    "flat_surface_aspect_undefined"
                    if aspect is None
                    else derived_status
                )

            grid_signature = raster_grid_signature(dataset)
            nodata_value = _nodata_value(dataset, self.band)

        common_details: dict[str, str | float | int] = {
            "source_item_id": tile.item_id,
            "source_asset_key": tile.asset_key,
            "source_filename": tile.source_filename,
            "source_href": tile.source_href,
            "manifest_path": str(self.tile_index.manifest_path),
            "source_crs": match.source_crs,
            "source_epsg": match.source_epsg or -1,
            "horizontal_crs": match.horizontal_crs,
            "horizontal_epsg": match.horizontal_epsg or -1,
            "vertical_crs": match.vertical_crs or "not_explicit",
            "vertical_epsg": match.vertical_epsg or -1,
            "pixel_row": row,
            "pixel_col": col,
            "pixel_resolution_x_m": pixel_size_x,
            "pixel_resolution_y_m": pixel_size_y,
            "scoring_status": "not_enabled",
        }
        if nodata_value is not None:
            common_details["nodata_value"] = nodata_value

        elevation_provenance = FeatureProvenance(
            source_name="lantmateriet_dtm_cog_terrain",
            quality=0.0 if center_nodata else 0.98,
            is_mock=False,
            semantic_status=(
                "central_pixel_nodata"
                if center_nodata
                else "validated_rh2000_elevation_m_not_scoring_enabled"
            ),
            source_path=str(tile.source_path),
            raw_value=raw_center,
            interpreted_value=elevation,
            is_nodata=center_nodata,
            grid_signature=grid_signature,
            details={**common_details, "unit": "metre_RH2000"},
        )
        derived_details = {
            **common_details,
            "method": HORN_METHOD,
            "neighborhood_size": NEIGHBORHOOD_SIZE,
            "aspect_convention": ASPECT_CONVENTION,
            "derived_status": derived_status,
        }
        slope_provenance = FeatureProvenance(
            source_name="lantmateriet_dtm_cog_terrain",
            quality=0.95 if slope is not None else 0.0,
            is_mock=False,
            semantic_status=derived_status,
            source_path=str(tile.source_path),
            interpreted_value=slope,
            is_nodata="nodata" in derived_status,
            grid_signature=grid_signature,
            details=derived_details,
        )
        aspect_provenance = FeatureProvenance(
            source_name="lantmateriet_dtm_cog_terrain",
            quality=0.95 if aspect is not None else (0.95 if slope == 0.0 else 0.0),
            is_mock=False,
            semantic_status=aspect_status,
            source_path=str(tile.source_path),
            interpreted_value=aspect,
            is_nodata="nodata" in aspect_status,
            grid_signature=grid_signature,
            details={**derived_details, "derived_status": aspect_status},
        )
        snapshot = FeatureSnapshot(
            features=StaticHabitatFeatures(
                elevation_m=elevation,
                slope_degrees=slope,
                aspect_degrees=aspect,
            ),
            metadata=DataSourceMetadata(
                source_name="lantmateriet_dtm_cog_terrain",
                quality=min(
                    elevation_provenance.quality,
                    slope_provenance.quality,
                    aspect_provenance.quality,
                ),
                is_mock=False,
                details={
                    "source_item_id": tile.item_id,
                    "method": HORN_METHOD,
                    "scoring_status": "not_enabled",
                },
            ),
            feature_provenance={
                "elevation_m": elevation_provenance,
                "slope_degrees": slope_provenance,
                "aspect_degrees": aspect_provenance,
            },
        )
        return TerrainSampleResult(snapshot=snapshot, tile_match=match)
