from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

import numpy as np
import pytest
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin, xy

from fungifind.data_sources.forest_raster import (
    ForestRasterDataSource,
    ForestShareInterpretation,
    HybridHabitatDataSource,
)
from fungifind.data_sources.mock import MockHabitatDataSource, MockWeatherDataSource
from fungifind.data_sources.raster import RasterPointOutsideBoundsError, RasterPointReader
from fungifind.models import Location, Species
from fungifind.service import MushroomScoringService


@dataclass(frozen=True)
class RasterFixture:
    path: Path
    value_location: Location
    nodata_location: Location
    expected_x: float
    expected_y: float


@pytest.fixture
def point_raster(tmp_path: Path) -> RasterFixture:
    crs = "EPSG:3006"
    value_location = Location(59.412, 18.132)
    to_raster = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    expected_x, expected_y = to_raster.transform(
        value_location.longitude, value_location.latitude
    )
    transform = from_origin(expected_x - 15, expected_y + 15, 10, 10)
    values = np.arange(16, dtype=np.int16).reshape(4, 4)
    values[1, 1] = 42
    values[2, 2] = -9999
    path = tmp_path / "point_lookup.tif"
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=4,
        height=4,
        count=1,
        dtype="int16",
        crs=crs,
        transform=transform,
        nodata=-9999,
    ) as dataset:
        dataset.write(values, 1)

    nodata_x, nodata_y = xy(transform, 2, 2)
    to_wgs84 = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    nodata_lon, nodata_lat = to_wgs84.transform(nodata_x, nodata_y)
    return RasterFixture(
        path=path,
        value_location=value_location,
        nodata_location=Location(nodata_lat, nodata_lon),
        expected_x=expected_x,
        expected_y=expected_y,
    )


def test_correct_pixel_lookup_and_dynamic_crs_transform(point_raster: RasterFixture) -> None:
    sample = RasterPointReader(point_raster.path).sample(point_raster.value_location)

    assert sample.source_crs == "EPSG:3006"
    assert sample.source_epsg == 3006
    assert sample.projected_x == pytest.approx(point_raster.expected_x, abs=0.001)
    assert sample.projected_y == pytest.approx(point_raster.expected_y, abs=0.001)
    assert (sample.pixel_row, sample.pixel_col) == (1, 1)
    assert sample.raw_value == 42
    assert sample.value == 42
    assert sample.is_nodata is False


def test_nodata_preserves_raw_value_but_returns_no_value(point_raster: RasterFixture) -> None:
    sample = RasterPointReader(point_raster.path).sample(point_raster.nodata_location)

    assert sample.raw_value == -9999
    assert sample.value is None
    assert sample.is_nodata is True


def test_batch_sampling_preserves_order_values_and_nodata(
    point_raster: RasterFixture,
) -> None:
    samples = RasterPointReader(point_raster.path).sample_many(
        [
            point_raster.value_location,
            point_raster.nodata_location,
            point_raster.value_location,
        ]
    )

    assert [sample.value for sample in samples] == [42, None, 42]
    assert [sample.is_nodata for sample in samples] == [False, True, False]
    assert samples[0].grid_signature == samples[2].grid_signature


def test_outside_bounds_raises_clear_error(point_raster: RasterFixture) -> None:
    with pytest.raises(RasterPointOutsideBoundsError, match="outside raster bounds"):
        RasterPointReader(point_raster.path).sample(Location(0.0, 0.0))


def test_reader_requests_only_a_one_pixel_window(
    point_raster: RasterFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    actual_open = rasterio.open
    requested_windows: list[Any] = []

    class RecordingDataset:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.dataset = actual_open(*args, **kwargs)

        def __enter__(self) -> Self:
            self.dataset.__enter__()
            return self

        def __exit__(self, *args: object) -> None:
            self.dataset.__exit__(*args)

        def __getattr__(self, name: str) -> Any:
            return getattr(self.dataset, name)

        def read(self, *args: Any, **kwargs: Any) -> np.ndarray:
            requested_windows.append(kwargs.get("window"))
            return self.dataset.read(*args, **kwargs)

    monkeypatch.setattr(
        "fungifind.data_sources.raster.rasterio.open",
        lambda *args, **kwargs: RecordingDataset(*args, **kwargs),
    )

    RasterPointReader(point_raster.path).sample(point_raster.value_location)

    assert len(requested_windows) == 1
    assert requested_windows[0].width == 1
    assert requested_windows[0].height == 1


def test_source_file_is_not_modified(point_raster: RasterFixture) -> None:
    before_bytes = point_raster.path.read_bytes()
    before_hash = hashlib.sha256(before_bytes).hexdigest()
    before_mtime = point_raster.path.stat().st_mtime_ns

    RasterPointReader(point_raster.path).sample(point_raster.value_location)

    after_hash = hashlib.sha256(point_raster.path.read_bytes()).hexdigest()
    assert after_hash == before_hash
    assert point_raster.path.stat().st_mtime_ns == before_mtime


def test_raw_default_is_safe_and_provisional_mode_is_explicit(
    point_raster: RasterFixture,
) -> None:
    raw_source = ForestRasterDataSource(point_raster.path)
    raw_snapshot = raw_source.get_features(point_raster.value_location)
    assert raw_snapshot.features.spruce_fraction is None
    assert raw_snapshot.feature_provenance["spruce_fraction"].raw_value == 42
    assert "unvalidated" in raw_snapshot.feature_provenance["spruce_fraction"].semantic_status

    raw_hybrid = HybridHabitatDataSource(raw_source, MockHabitatDataSource()).get_features(
        point_raster.value_location
    )
    assert raw_hybrid.features.spruce_fraction is not None
    assert (
        raw_hybrid.feature_provenance["spruce_fraction"].details["real_candidate_raw_value"]
        == 42
    )

    provisional_source = ForestRasterDataSource(
        point_raster.path,
        interpretation=ForestShareInterpretation.PROVISIONAL_ZERO_TO_HUNDRED_SHARE,
    )
    provisional = provisional_source.get_features(point_raster.value_location)
    assert provisional.features.spruce_fraction == pytest.approx(0.42)
    assert provisional.feature_provenance["spruce_fraction"].raw_value == 42
    assert provisional.feature_provenance["spruce_fraction"].interpreted_value == 0.42


def test_hybrid_source_reaches_existing_service_with_feature_provenance(
    point_raster: RasterFixture,
) -> None:
    real = ForestRasterDataSource(
        point_raster.path,
        interpretation=ForestShareInterpretation.PROVISIONAL_ZERO_TO_HUNDRED_SHARE,
    )
    hybrid = HybridHabitatDataSource(real, MockHabitatDataSource())
    snapshot = hybrid.get_features(point_raster.value_location)
    assert snapshot.features.spruce_fraction == pytest.approx(0.42)
    assert snapshot.feature_provenance["spruce_fraction"].is_mock is False
    assert snapshot.feature_provenance["pine_fraction"].is_mock is True

    result = MushroomScoringService(hybrid, MockWeatherDataSource()).get_score(
        point_raster.value_location.latitude,
        point_raster.value_location.longitude,
        "2026-08-23",
        Species.CANTHARELLUS_CIBARIUS,
    )
    assert result.data_sources["habitat"] == "hybrid_real_raster_and_mock_habitat_v0"
    assert result.feature_provenance["spruce_fraction"].raw_value == 42
    assert result.feature_provenance["spruce_fraction"].is_mock is False
