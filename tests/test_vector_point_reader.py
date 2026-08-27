from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest
from pyproj import Transformer
from shapely.geometry import MultiPolygon, box
from shapely.geometry.base import BaseGeometry

from fungifind.data_sources.vector import (
    LOOKUP_METHOD,
    GeoPackageVectorPointReader,
    VectorSpatialIndexError,
)
from fungifind.models import Location

GpkgFactory = Callable[
    ...,
    Path,
]


def _feature(
    feature_id: int,
    geometry: BaseGeometry,
    code: int,
    label: str,
) -> tuple[int, BaseGeometry, Mapping[str, str | float | int | None]]:
    return (
        feature_id,
        MultiPolygon([geometry]),
        {
            "jg2": code,
            "jg2_tx": label,
            "kartering": "synthetic",
            "karttyp": 1,
            "symbol": code,
            "objectid": feature_id,
            "geom_area": geometry.area,
            "geom_length": geometry.length,
        },
    )


def test_wgs84_is_transformed_and_rtree_limits_exact_polygon_candidates(
    synthetic_gpkg_factory: GpkgFactory,
) -> None:
    longitude, latitude = 18.132, 59.412
    transformer = Transformer.from_crs(4326, 3006, always_xy=True)
    x, y = transformer.transform(longitude, latitude)
    path = synthetic_gpkg_factory(
        epsg=3006,
        features=(
            _feature(7, box(x - 50, y - 50, x + 50, y + 50), 100, "Morän"),
            _feature(8, box(x + 10_000, y, x + 10_100, y + 100), 75, "Torv"),
        ),
    )

    sample = GeoPackageVectorPointReader(
        path, "grundlager", selected_attributes=("jg2", "jg2_tx")
    ).sample(Location(latitude, longitude))

    assert sample.found
    assert sample.feature_id == 7
    assert sample.attributes == {"jg2": 100, "jg2_tx": "Morän"}
    assert sample.source_epsg == 3006
    assert sample.projected_x == pytest.approx(x)
    assert sample.projected_y == pytest.approx(y)
    assert sample.candidate_count == 1
    assert sample.matching_feature_count == 1
    assert sample.spatial_index_used is True
    assert sample.lookup_method == LOOKUP_METHOD


def test_outside_all_polygons_returns_a_deterministic_no_feature_sample(
    synthetic_gpkg_factory: GpkgFactory,
) -> None:
    path = synthetic_gpkg_factory(
        epsg=4326,
        features=(_feature(1, box(17.0, 58.0, 17.1, 58.1), 100, "Morän"),),
    )

    sample = GeoPackageVectorPointReader(path, "grundlager").sample(Location(59.0, 18.0))

    assert sample.found is False
    assert sample.feature_id is None
    assert sample.attributes == {}
    assert sample.candidate_count == 0
    assert sample.matching_feature_count == 0


def test_boundary_and_overlap_use_covers_then_lowest_fid(
    synthetic_gpkg_factory: GpkgFactory,
) -> None:
    path = synthetic_gpkg_factory(
        epsg=4326,
        features=(
            _feature(9, box(-1.0, -1.0, 0.0, 1.0), 75, "Torv"),
            _feature(3, box(0.0, -1.0, 1.0, 1.0), 100, "Morän"),
        ),
    )

    sample = GeoPackageVectorPointReader(path, "grundlager").sample(Location(0.0, 0.0))

    assert sample.candidate_count == 2
    assert sample.matching_feature_count == 2
    assert sample.feature_id == 3
    assert sample.attributes["jg2"] == 100


def test_reader_requires_a_registered_spatial_index(
    synthetic_gpkg_factory: GpkgFactory,
) -> None:
    path = synthetic_gpkg_factory(
        epsg=4326,
        features=(_feature(1, box(0.0, 0.0, 1.0, 1.0), 100, "Morän"),),
        include_rtree=False,
    )

    with pytest.raises(VectorSpatialIndexError, match="RTree"):
        GeoPackageVectorPointReader(path, "grundlager")


def test_read_only_lookup_does_not_change_the_source_geopackage(
    synthetic_gpkg_factory: GpkgFactory,
) -> None:
    path = synthetic_gpkg_factory(
        epsg=4326,
        features=(_feature(1, box(0.0, 0.0, 1.0, 1.0), 100, "Morän"),),
    )
    before_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    before_stat = path.stat()

    GeoPackageVectorPointReader(path, "grundlager").sample(Location(0.5, 0.5))

    after_stat = path.stat()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before_hash
    assert after_stat.st_size == before_stat.st_size
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
