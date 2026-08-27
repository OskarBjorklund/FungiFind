from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from typing import Any, Self

import pytest

SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "download_elevation.py"
SPEC = importlib.util.spec_from_file_location("download_elevation", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
download_elevation = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = download_elevation
SPEC.loader.exec_module(download_elevation)


def _collection(
    collection_id: str,
    *,
    title: str,
    bbox: list[float],
    canonical: bool,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": collection_id,
        "title": title,
        "description": "Markhöjdmodell som höjdgrid i GeoTIFF/COG",
        "extent": {"spatial": {"bbox": [bbox]}},
        "stac_version": "1.1.0" if canonical else "1.0.0",
        "license": "CC-BY-4.0",
    }
    if canonical:
        result["item_assets"] = {
            "data": {
                "type": "image/tiff; application=geotiff; profile=cloud-optimized",
                "roles": ["data"],
            }
        }
    return result


class _FakeClient:
    def __init__(self, collections: list[dict[str, Any]]) -> None:
        self.collections = collections

    def list_collections(self) -> list[dict[str, Any]]:
        return self.collections

    def get_collection(self, collection_id: str) -> dict[str, Any]:
        return next(item for item in self.collections if item["id"] == collection_id)


def _item() -> dict[str, Any]:
    return {
        "id": "659_67",
        "collection": "dtm-cog",
        "bbox": [17.9, 59.4, 18.2, 59.5],
        "links": [{"rel": "self", "href": "https://example.test/items/659_67"}],
        "properties": {
            "datetime": "2023-04-27T00:00:00Z",
            "start_datetime": "2021-03-23T00:00:00Z",
            "end_datetime": "2025-05-31T00:00:00Z",
            "proj:code": "EPSG:5845",
        },
        "assets": {
            "thumbnail": {
                "href": "https://example.test/tile.png",
                "type": "image/png",
                "roles": ["thumbnail", "overview"],
            },
            "metadata": {
                "href": "https://example.test/tile.json",
                "type": "application/json",
                "roles": ["metadata"],
            },
            "breakgeometry": {
                "href": "https://example.test/tile.gpkg",
                "type": "application/geopackage+sqlite3",
                "roles": ["data"],
            },
            "data": {
                "href": "https://example.test/tile.tif",
                "type": "image/tiff; application=geotiff; profile=cloud-optimized",
                "roles": ["data"],
                "title": "tile.tif",
                "file:size": 4,
                "proj:bbox": [670000, 6590000, 680000, 6600000],
                "file:checksum": "sha256:"
                + hashlib.sha256(b"test").hexdigest(),
            },
        },
    }


def test_bbox_validation() -> None:
    download_elevation.validate_bbox((18.1, 59.4, 18.2, 59.5))
    with pytest.raises(ValueError, match="min-lon"):
        download_elevation.validate_bbox((18.2, 59.4, 18.1, 59.5))


def test_collection_discovery_prefers_canonical_advertised_cog() -> None:
    regional = _collection(
        "mhm-65_6",
        title="Markhöjdmodell 65_6",
        bbox=[16.7, 58.5, 18.6, 59.6],
        canonical=False,
    )
    canonical = _collection(
        "dtm-cog",
        title="Markhöjdmodell",
        bbox=[9.0, 55.0, 25.6, 69.1],
        canonical=True,
    )
    unrelated = {
        "id": "dsm-copc",
        "title": "Laserdata Skog",
        "description": "Ytmodell som punktmoln",
        "extent": {"spatial": {"bbox": [[9.0, 55.0, 25.6, 69.1]]}},
    }

    result = download_elevation.discover_collection(
        _FakeClient([regional, unrelated, canonical]),
        (18.131, 59.411, 18.133, 59.413),
    )

    assert result.selected["id"] == "dtm-cog"
    assert {item["id"] for item in result.intersecting_elevation} == {
        "dtm-cog",
        "mhm-65_6",
    }


def test_asset_selection_and_manifest_fields() -> None:
    item = _item()
    key, asset = download_elevation.select_elevation_asset(item)
    plans = download_elevation.build_asset_plans([item])

    assert key == "data"
    assert asset["roles"] == ["data"]
    assert len(plans) == 1
    plan = plans[0]
    assert plan.local_filename == "tile.tif"
    assert plan.proj_epsg == 5845
    assert plan.proj_bbox == [670000.0, 6590000.0, 680000.0, 6600000.0]
    record = plan.manifest_record(status="downloaded", actual_size=4)
    for required in (
        "item_id",
        "asset_key",
        "source_href",
        "local_filename",
        "datetime",
        "start_datetime",
        "end_datetime",
        "proj:epsg",
        "proj:bbox",
        "file_size",
        "checksum",
    ):
        assert required in record


class _Response:
    status = 200

    def __init__(self, payload: bytes, declared_length: int) -> None:
        self.payload = payload
        self.headers = {"Content-Length": str(declared_length)}
        self._read = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        if self._read:
            return b""
        self._read = True
        return self.payload


def test_stream_download_rejects_and_removes_incomplete_file(tmp_path: Path) -> None:
    plan = download_elevation.build_asset_plans([_item()])[0]
    destination = tmp_path / plan.local_filename

    with pytest.raises(download_elevation.ElevationDownloadError, match="Incomplete"):
        download_elevation.stream_download(
            plan,
            destination,
            timeout=1,
            opener=lambda request, timeout: _Response(b"tes", declared_length=4),
        )

    assert not destination.exists()
    assert not destination.with_name(destination.name + ".part").exists()


def test_stream_download_skips_existing_validated_file(tmp_path: Path) -> None:
    plan = download_elevation.build_asset_plans([_item()])[0]
    destination = tmp_path / plan.local_filename
    destination.write_bytes(b"test")

    status, size = download_elevation.stream_download(
        plan,
        destination,
        timeout=1,
        opener=lambda request, timeout: pytest.fail("network should not be used"),
    )

    assert status == "already_present_validated"
    assert size == 4
