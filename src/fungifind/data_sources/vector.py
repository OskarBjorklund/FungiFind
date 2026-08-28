"""Read one WGS84 point from an indexed polygon layer in a GeoPackage."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any

from pyproj import CRS, Transformer

from fungifind.models import Location

try:
    from shapely import from_wkb
    from shapely.geometry import Point
except ImportError as exc:  # pragma: no cover - exercised only without the geo extra
    raise ImportError(
        "GeoPackage point sampling requires Shapely; install fungifind[geo]"
    ) from exc


LOOKUP_METHOD = "gpkg_rtree_bbox_then_shapely_covers_lowest_fid"


class VectorPointError(RuntimeError):
    """Base error for vector point lookups."""


class VectorLayerError(VectorPointError):
    """Raised when a requested GeoPackage layer is not a supported polygon layer."""


class VectorSpatialIndexError(VectorPointError):
    """Raised when the required GeoPackage RTree index is unavailable."""


@dataclass(frozen=True, slots=True)
class GeoPackageLayerInfo:
    """Schema and CRS information needed for indexed point lookups."""

    source_path: str
    layer_name: str
    geometry_column: str
    geometry_type: str
    fid_column: str
    attribute_columns: tuple[str, ...]
    source_crs: str
    source_epsg: int | None
    rtree_table: str


@dataclass(frozen=True, slots=True)
class VectorPointSample:
    """One deterministic polygon lookup, including the no-feature case."""

    source_path: str
    layer_name: str
    source_crs: str
    source_epsg: int | None
    projected_x: float
    projected_y: float
    feature_id: int | None
    attributes: Mapping[str, str | float | int | None]
    candidate_count: int
    matching_feature_count: int
    spatial_index_used: bool
    lookup_method: str = LOOKUP_METHOD

    @property
    def found(self) -> bool:
        return self.feature_id is not None


def _quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _read_gpkg_geometry(payload: bytes) -> Any:
    """Strip the GeoPackage binary header and decode the embedded WKB geometry."""

    if len(payload) < 8 or payload[:2] != b"GP":
        raise VectorLayerError("Feature geometry is not a GeoPackage binary geometry")
    flags = payload[3]
    envelope_code = (flags >> 1) & 0b111
    envelope_coordinate_counts = {0: 0, 1: 4, 2: 6, 3: 6, 4: 8}
    if envelope_code not in envelope_coordinate_counts:
        raise VectorLayerError(f"Unsupported GeoPackage envelope code: {envelope_code}")
    wkb_offset = 8 + envelope_coordinate_counts[envelope_code] * 8
    if len(payload) <= wkb_offset:
        raise VectorLayerError("GeoPackage geometry has no embedded WKB payload")
    return from_wkb(payload[wkb_offset:])


class GeoPackageVectorPointReader:
    """Use a GeoPackage RTree and exact polygon coverage for one WGS84 point.

    Boundary points count as matches (`covers`, rather than `contains`). If several
    polygons cover the point, the feature with the lowest integer primary key wins.
    """

    def __init__(
        self,
        source_path: str | Path,
        layer_name: str,
        *,
        selected_attributes: Sequence[str] | None = None,
    ) -> None:
        path = Path(source_path).resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        self.path = path
        self.layer_name = layer_name
        self._connection_uri = path.as_uri() + "?mode=ro&immutable=1"
        self._lock = RLock()
        self._connection: sqlite3.Connection | None = None
        self.layer_info = self._inspect_layer(selected_attributes)
        source_crs = CRS.from_user_input(self.layer_info.source_crs)
        self._transformer = Transformer.from_crs("EPSG:4326", source_crs, always_xy=True)

    def _new_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._connection_uri,
            uri=True,
            check_same_thread=False,
        )
        connection.execute("PRAGMA query_only=ON")
        return connection

    def _reader_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            self._connection = self._new_connection()
        return self._connection

    def close(self) -> None:
        """Close the reusable immutable GeoPackage connection."""

        with self._lock:
            if self._connection is not None:
                self._connection.close()
            self._connection = None

    def _inspect_layer(
        self, selected_attributes: Sequence[str] | None
    ) -> GeoPackageLayerInfo:
        with closing(self._new_connection()) as connection:
            geometry_row = connection.execute(
                """
                SELECT column_name, geometry_type_name, srs_id
                FROM gpkg_geometry_columns
                WHERE table_name = ?
                """,
                (self.layer_name,),
            ).fetchone()
            if geometry_row is None:
                raise VectorLayerError(
                    f"GeoPackage has no feature layer named {self.layer_name!r}"
                )
            geometry_column, geometry_type, srs_id = geometry_row
            if str(geometry_type).upper() not in {"POLYGON", "MULTIPOLYGON"}:
                raise VectorLayerError(
                    f"Layer {self.layer_name!r} must contain polygons, got {geometry_type}"
                )

            table_info = connection.execute(
                f"PRAGMA table_info({_quoted(self.layer_name)})"
            ).fetchall()
            primary_keys = [str(row[1]) for row in table_info if int(row[5]) > 0]
            if len(primary_keys) != 1:
                raise VectorLayerError(
                    f"Layer {self.layer_name!r} must have exactly one primary-key/FID column"
                )
            fid_column = primary_keys[0]
            available_attributes = tuple(
                str(row[1])
                for row in table_info
                if str(row[1]) not in {fid_column, str(geometry_column)}
            )
            chosen_attributes = (
                available_attributes
                if selected_attributes is None
                else tuple(selected_attributes)
            )
            unknown = sorted(set(chosen_attributes) - set(available_attributes))
            if unknown:
                raise VectorLayerError(
                    f"Unknown attributes for layer {self.layer_name!r}: {unknown}"
                )
            if len(set(chosen_attributes)) != len(chosen_attributes):
                raise VectorLayerError("Selected vector attributes must be unique")

            rtree_table = f"rtree_{self.layer_name}_{geometry_column}"
            rtree_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (rtree_table,),
            ).fetchone()
            extension_exists = connection.execute(
                """
                SELECT 1 FROM gpkg_extensions
                WHERE table_name=? AND column_name=?
                  AND extension_name='gpkg_rtree_index'
                """,
                (self.layer_name, geometry_column),
            ).fetchone()
            if rtree_exists is None or extension_exists is None:
                raise VectorSpatialIndexError(
                    f"Layer {self.layer_name!r} has no registered GeoPackage RTree index"
                )

            srs_row = connection.execute(
                """
                SELECT organization, organization_coordsys_id, definition
                FROM gpkg_spatial_ref_sys WHERE srs_id=?
                """,
                (srs_id,),
            ).fetchone()
            if srs_row is None:
                raise VectorLayerError(f"Missing GeoPackage CRS definition for SRS {srs_id}")
            organization, organization_code, definition = srs_row
            if str(organization).upper() == "EPSG" and int(organization_code) > 0:
                crs = CRS.from_epsg(int(organization_code))
            else:
                crs = CRS.from_wkt(str(definition))

        return GeoPackageLayerInfo(
            source_path=str(self.path),
            layer_name=self.layer_name,
            geometry_column=str(geometry_column),
            geometry_type=str(geometry_type),
            fid_column=fid_column,
            attribute_columns=chosen_attributes,
            source_crs=crs.to_string(),
            source_epsg=crs.to_epsg(),
            rtree_table=rtree_table,
        )

    def sample(self, location: Location) -> VectorPointSample:
        """Transform a WGS84 coordinate and return one indexed polygon match."""

        projected_x, projected_y = self._transformer.transform(
            location.longitude, location.latitude
        )
        info = self.layer_info
        selected_sql = ", ".join(
            f"f.{_quoted(attribute)}" for attribute in info.attribute_columns
        )
        if selected_sql:
            selected_sql = ", " + selected_sql
        query = f"""
            SELECT f.{_quoted(info.fid_column)}, f.{_quoted(info.geometry_column)}
                   {selected_sql}
            FROM {_quoted(info.layer_name)} AS f
            JOIN {_quoted(info.rtree_table)} AS r
              ON f.{_quoted(info.fid_column)} = r.id
            WHERE r.minx <= ? AND r.maxx >= ?
              AND r.miny <= ? AND r.maxy >= ?
            ORDER BY f.{_quoted(info.fid_column)} ASC
        """
        with self._lock:
            connection = self._reader_connection()
            candidates = connection.execute(
                query,
                (projected_x, projected_x, projected_y, projected_y),
            ).fetchall()

        point = Point(projected_x, projected_y)
        matches: list[tuple[Any, ...]] = []
        for candidate in candidates:
            geometry = _read_gpkg_geometry(candidate[1])
            if geometry.covers(point):
                matches.append(candidate)

        if matches:
            selected = matches[0]
            feature_id = int(selected[0])
            attributes = dict(zip(info.attribute_columns, selected[2:], strict=True))
        else:
            feature_id = None
            attributes = {}

        return VectorPointSample(
            source_path=str(self.path),
            layer_name=info.layer_name,
            source_crs=info.source_crs,
            source_epsg=info.source_epsg,
            projected_x=float(projected_x),
            projected_y=float(projected_y),
            feature_id=feature_id,
            attributes=attributes,
            candidate_count=len(candidates),
            matching_feature_count=len(matches),
            spatial_index_used=True,
        )
