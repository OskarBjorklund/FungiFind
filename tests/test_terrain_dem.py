from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin, xy

from fungifind.data_sources import (
    ElevationTileIndex,
    ElevationTileNotFoundError,
    TerrainDemReader,
)
from fungifind.data_sources.raster import horizontal_crs
from fungifind.models import Location, Species
from fungifind.service import get_mushroom_score


def _write_dem(
    path: Path,
    location: Location,
    values: np.ndarray,
    *,
    crs: str = "EPSG:3006",
    pixel_size: float = 10.0,
    nodata: float = -9999.0,
) -> None:
    target = horizontal_crs(crs)
    transformer = Transformer.from_crs("EPSG:4326", target, always_xy=True)
    center_x, center_y = transformer.transform(location.longitude, location.latitude)
    height, width = values.shape
    transform = from_origin(
        center_x - width * pixel_size / 2,
        center_y + height * pixel_size / 2,
        pixel_size,
        pixel_size,
    )
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=width,
        height=height,
        count=1,
        dtype="float32",
        crs=crs,
        transform=transform,
        nodata=nodata,
    ) as dataset:
        dataset.write(values.astype(np.float32), 1)
        dataset.set_band_unit(1, "metre")


def _manifest_record(
    path: Path,
    *,
    item_id: str,
    location: Location,
    proj_epsg: int = 3006,
) -> dict[str, object]:
    return {
        "collection_id": "synthetic-dtm",
        "item_id": item_id,
        "asset_key": "data",
        "source_href": f"https://example.test/{path.name}",
        "local_filename": path.name,
        "media_type": "image/tiff; application=geotiff",
        "roles": ["data"],
        "item_bbox_wgs84": [
            location.longitude - 0.2,
            location.latitude - 0.2,
            location.longitude + 0.2,
            location.latitude + 0.2,
        ],
        "proj:epsg": proj_epsg,
        "proj:bbox": [0, 0, 1, 1],
    }


def _write_manifest(path: Path, records: list[dict[str, object]]) -> Path:
    manifest_path = path / "manifest.json"
    manifest_path.write_text(
        json.dumps({"schema_version": 1, "assets": records}),
        encoding="utf-8",
    )
    return manifest_path


def _reader_for_surface(
    tmp_path: Path,
    values: np.ndarray,
    *,
    crs: str = "EPSG:3006",
    item_id: str = "synthetic-tile",
) -> tuple[TerrainDemReader, Path, Location]:
    location = Location(59.412, 18.132)
    raster_path = tmp_path / f"{item_id}.tif"
    _write_dem(raster_path, location, values, crs=crs)
    epsg = 5845 if crs == "EPSG:5845" else int(crs.split(":", 1)[1])
    manifest = _write_manifest(
        tmp_path,
        [_manifest_record(raster_path, item_id=item_id, location=location, proj_epsg=epsg)],
    )
    return TerrainDemReader.from_manifest(manifest), raster_path, location


def _location_for_pixel(path: Path, row: int, col: int) -> Location:
    with rasterio.open(path) as dataset:
        x_coord, y_coord = xy(dataset.transform, row, col)
        transformer = Transformer.from_crs(
            horizontal_crs(dataset.crs),
            "EPSG:4326",
            always_xy=True,
        )
        longitude, latitude = transformer.transform(x_coord, y_coord)
    return Location(latitude, longitude)


def test_flat_surface_has_zero_slope_and_undefined_aspect(tmp_path: Path) -> None:
    reader, _, location = _reader_for_surface(
        tmp_path,
        np.full((5, 5), 100.0),
    )

    result = reader.sample_terrain(location)
    provenance = result.snapshot.feature_provenance

    assert result.snapshot.features.elevation_m == pytest.approx(100.0)
    assert result.snapshot.features.slope_degrees == pytest.approx(0.0)
    assert result.snapshot.features.aspect_degrees is None
    assert provenance["elevation_m"].details["source_item_id"] == "synthetic-tile"
    assert provenance["slope_degrees"].details["method"].startswith("Horn 3x3")
    assert provenance["slope_degrees"].details["neighborhood_size"] == 3
    assert provenance["aspect_degrees"].semantic_status == (
        "flat_surface_aspect_undefined"
    )


def test_east_facing_surface(tmp_path: Path) -> None:
    values = np.repeat((100.0 - np.arange(5) * 10.0)[None, :], 5, axis=0)
    reader, _, location = _reader_for_surface(tmp_path, values)

    result = reader.sample_terrain(location)

    assert result.snapshot.features.slope_degrees == pytest.approx(45.0, abs=1e-6)
    assert result.snapshot.features.aspect_degrees == pytest.approx(90.0, abs=1e-6)


def test_north_facing_surface(tmp_path: Path) -> None:
    values = np.repeat((100.0 + np.arange(5) * 10.0)[:, None], 5, axis=1)
    reader, _, location = _reader_for_surface(tmp_path, values)

    result = reader.sample_terrain(location)

    assert result.snapshot.features.slope_degrees == pytest.approx(45.0, abs=1e-6)
    assert result.snapshot.features.aspect_degrees == pytest.approx(0.0, abs=1e-6)


def test_central_nodata_removes_all_terrain_features(tmp_path: Path) -> None:
    values = np.full((5, 5), 100.0)
    values[2, 2] = -9999.0
    reader, _, location = _reader_for_surface(tmp_path, values)

    result = reader.sample_terrain(location)
    elevation = result.snapshot.feature_provenance["elevation_m"]

    assert result.snapshot.features.elevation_m is None
    assert result.snapshot.features.slope_degrees is None
    assert result.snapshot.features.aspect_degrees is None
    assert elevation.raw_value == -9999.0
    assert elevation.is_nodata is True


def test_neighbor_nodata_preserves_elevation_but_not_derivatives(tmp_path: Path) -> None:
    values = np.full((5, 5), 100.0)
    values[1, 2] = -9999.0
    reader, _, location = _reader_for_surface(tmp_path, values)

    result = reader.sample_terrain(location)
    slope = result.snapshot.feature_provenance["slope_degrees"]

    assert result.snapshot.features.elevation_m == pytest.approx(100.0)
    assert result.snapshot.features.slope_degrees is None
    assert result.snapshot.features.aspect_degrees is None
    assert slope.semantic_status == "neighborhood_contains_nodata"


def test_tile_edge_preserves_center_but_has_no_derivatives(tmp_path: Path) -> None:
    reader, raster_path, _ = _reader_for_surface(
        tmp_path,
        np.full((5, 5), 75.0),
    )
    edge_location = _location_for_pixel(raster_path, 0, 0)

    result = reader.sample_terrain(edge_location)

    assert result.snapshot.features.elevation_m == pytest.approx(75.0)
    assert result.snapshot.features.slope_degrees is None
    assert result.snapshot.features.aspect_degrees is None
    assert (
        result.snapshot.feature_provenance["slope_degrees"].semantic_status
        == "insufficient_neighborhood_at_tile_edge"
    )


@pytest.mark.parametrize(
    ("crs", "source_epsg", "horizontal_epsg", "vertical_epsg"),
    [
        ("EPSG:3857", 3857, 3857, None),
        ("EPSG:5845", 5845, 3006, 5613),
    ],
)
def test_crs_transformation_and_compound_horizontal_component(
    tmp_path: Path,
    crs: str,
    source_epsg: int,
    horizontal_epsg: int,
    vertical_epsg: int | None,
) -> None:
    reader, _, location = _reader_for_surface(
        tmp_path,
        np.full((5, 5), 123.0),
        crs=crs,
    )

    result = reader.sample_terrain(location)
    match = result.tile_match

    assert result.snapshot.features.elevation_m == pytest.approx(123.0)
    assert match.source_epsg == source_epsg
    assert match.horizontal_epsg == horizontal_epsg
    assert match.vertical_epsg == vertical_epsg


def test_tile_index_selects_correct_tile_from_multiple_files(tmp_path: Path) -> None:
    first_location = Location(59.412, 18.132)
    second_location = Location(57.250, 14.600)
    first_path = tmp_path / "first.tif"
    second_path = tmp_path / "second.tif"
    _write_dem(first_path, first_location, np.full((5, 5), 10.0))
    _write_dem(second_path, second_location, np.full((5, 5), 20.0))
    manifest = _write_manifest(
        tmp_path,
        [
            _manifest_record(first_path, item_id="first", location=first_location),
            _manifest_record(second_path, item_id="second", location=second_location),
        ],
    )
    index = ElevationTileIndex(manifest)

    assert index.find_tile(first_location).tile.item_id == "first"
    assert index.find_tile(second_location).tile.item_id == "second"
    with pytest.raises(ElevationTileNotFoundError, match="No downloaded"):
        index.find_tile(Location(0.0, 0.0))


def test_source_raster_is_unchanged_and_scoring_is_not_connected(tmp_path: Path) -> None:
    reader, raster_path, location = _reader_for_surface(
        tmp_path,
        np.full((5, 5), 88.0),
    )
    before_hash = hashlib.sha256(raster_path.read_bytes()).hexdigest()
    before_mtime = raster_path.stat().st_mtime_ns
    baseline = get_mushroom_score(
        location.latitude,
        location.longitude,
        "2026-08-23",
        Species.CANTHARELLUS_CIBARIUS,
    )
    assert baseline.habitat_score == 0.951268
    assert baseline.final_score == 0.970761
    assert baseline.confidence == 0.41

    reader.sample_terrain(location)
    after = get_mushroom_score(
        location.latitude,
        location.longitude,
        "2026-08-23",
        Species.CANTHARELLUS_CIBARIUS,
    )

    assert not hasattr(reader, "get_features")
    assert after == baseline
    assert hashlib.sha256(raster_path.read_bytes()).hexdigest() == before_hash
    assert raster_path.stat().st_mtime_ns == before_mtime
