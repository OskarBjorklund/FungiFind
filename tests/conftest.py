from __future__ import annotations

import sqlite3
import struct
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest
from pyproj import CRS
from shapely import to_wkb
from shapely.geometry.base import BaseGeometry


@pytest.fixture
def synthetic_gpkg_factory(
    tmp_path: Path,
) -> Callable[..., Path]:
    """Create a minimal indexed polygon GeoPackage without GDAL/Fiona writes."""

    def create(
        *,
        epsg: int,
        features: Sequence[
            tuple[int, BaseGeometry, Mapping[str, str | float | int | None]]
        ],
        layer: str = "grundlager",
        include_rtree: bool = True,
        filename: str = "synthetic.gpkg",
    ) -> Path:
        path = tmp_path / filename
        crs = CRS.from_epsg(epsg)
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE gpkg_spatial_ref_sys (
                srs_name TEXT NOT NULL,
                srs_id INTEGER NOT NULL PRIMARY KEY,
                organization TEXT NOT NULL,
                organization_coordsys_id INTEGER NOT NULL,
                definition TEXT NOT NULL,
                description TEXT
            );
            CREATE TABLE gpkg_contents (
                table_name TEXT NOT NULL PRIMARY KEY,
                data_type TEXT NOT NULL,
                identifier TEXT UNIQUE,
                description TEXT DEFAULT '',
                last_change DATETIME NOT NULL,
                min_x DOUBLE,
                min_y DOUBLE,
                max_x DOUBLE,
                max_y DOUBLE,
                srs_id INTEGER
            );
            CREATE TABLE gpkg_geometry_columns (
                table_name TEXT NOT NULL,
                column_name TEXT NOT NULL,
                geometry_type_name TEXT NOT NULL,
                srs_id INTEGER NOT NULL,
                z TINYINT NOT NULL,
                m TINYINT NOT NULL,
                PRIMARY KEY (table_name, column_name)
            );
            CREATE TABLE gpkg_extensions (
                table_name TEXT,
                column_name TEXT,
                extension_name TEXT NOT NULL,
                definition TEXT NOT NULL,
                scope TEXT NOT NULL,
                UNIQUE (table_name, column_name, extension_name)
            );
            """
        )
        connection.execute(
            "INSERT INTO gpkg_spatial_ref_sys VALUES (?, ?, 'EPSG', ?, ?, '')",
            (crs.name, epsg, epsg, crs.to_wkt()),
        )
        connection.execute(
            f"""
            CREATE TABLE "{layer}" (
                id INTEGER NOT NULL PRIMARY KEY,
                geom BLOB NOT NULL,
                jg2 INTEGER,
                jg2_tx TEXT,
                kartering TEXT,
                karttyp INTEGER,
                symbol INTEGER,
                objectid INTEGER,
                geom_area REAL,
                geom_length REAL
            )
            """
        )
        connection.execute(
            "INSERT INTO gpkg_geometry_columns VALUES (?, 'geom', 'MULTIPOLYGON', ?, 0, 0)",
            (layer, epsg),
        )

        if features:
            total_bounds = [
                min(geometry.bounds[0] for _, geometry, _ in features),
                min(geometry.bounds[1] for _, geometry, _ in features),
                max(geometry.bounds[2] for _, geometry, _ in features),
                max(geometry.bounds[3] for _, geometry, _ in features),
            ]
        else:
            total_bounds = [None, None, None, None]
        connection.execute(
            """
            INSERT INTO gpkg_contents
            VALUES (?, 'features', ?, '', '2026-08-27T00:00:00.000Z', ?, ?, ?, ?, ?)
            """,
            (layer, layer, *total_bounds, epsg),
        )

        rtree_table = f"rtree_{layer}_geom"
        if include_rtree:
            connection.execute(
                f"CREATE VIRTUAL TABLE \"{rtree_table}\" USING rtree(id,minx,maxx,miny,maxy)"
            )
            connection.execute(
                """
                INSERT INTO gpkg_extensions VALUES (
                    ?, 'geom', 'gpkg_rtree_index',
                    'http://www.geopackage.org/spec/#extension_rtree', 'write-only'
                )
                """,
                (layer,),
            )

        attribute_names = (
            "jg2",
            "jg2_tx",
            "kartering",
            "karttyp",
            "symbol",
            "objectid",
            "geom_area",
            "geom_length",
        )
        for feature_id, geometry, attributes in features:
            # Version 0, little-endian header, no envelope, followed by WKB.
            payload = b"GP" + bytes((0, 1)) + struct.pack("<i", epsg) + to_wkb(
                geometry, byte_order=1
            )
            values = [attributes.get(name) for name in attribute_names]
            connection.execute(
                f"INSERT INTO \"{layer}\" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (feature_id, payload, *values),
            )
            if include_rtree:
                min_x, min_y, max_x, max_y = geometry.bounds
                connection.execute(
                    f"INSERT INTO \"{rtree_table}\" VALUES (?, ?, ?, ?, ?)",
                    (feature_id, min_x, max_x, min_y, max_y),
                )
        connection.commit()
        connection.close()
        return path

    return create

