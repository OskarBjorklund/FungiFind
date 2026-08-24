from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin

from fungifind.data_sources import (
    ForestProfileRasterDataSource,
    ForestShareInterpretation,
    HybridHabitatDataSource,
    MockHabitatDataSource,
    MockWeatherDataSource,
    RasterPointReader,
    diagnose_tree_fractions,
)
from fungifind.models import Location, StaticHabitatFeatures
from fungifind.service import MushroomScoringService

PROFILE_VALUES = {
    "spruce_fraction": 40,
    "pine_fraction": 30,
    "birch_fraction": 20,
    "other_deciduous_fraction": 10,
}


def _profile_rasters(
    tmp_path: Path,
    location: Location,
    values: dict[str, int] | None = None,
) -> dict[str, Path]:
    crs = "EPSG:3006"
    to_raster = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    x, y = to_raster.transform(location.longitude, location.latitude)
    transform = from_origin(x - 15, y + 15, 10, 10)
    paths: dict[str, Path] = {}
    for name, raw_value in (values or PROFILE_VALUES).items():
        data = np.zeros((4, 4), dtype=np.int16)
        data[1, 1] = raw_value
        path = tmp_path / f"{name}.tif"
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
            nodata=-1,
        ) as dataset:
            dataset.write(data, 1)
        paths[name] = path
    return paths


def test_multiple_rasters_map_to_features_and_reuse_generic_reader(tmp_path: Path) -> None:
    location = Location(59.412, 18.132)
    source = ForestProfileRasterDataSource(
        _profile_rasters(tmp_path, location),
        interpretation=ForestShareInterpretation.PROVISIONAL_ZERO_TO_HUNDRED_SHARE,
    )

    assert all(isinstance(layer.reader, RasterPointReader) for layer in source.sources.values())
    result = source.sample_profile(location)
    assert result.snapshot.features.spruce_fraction == pytest.approx(0.40)
    assert result.snapshot.features.pine_fraction == pytest.approx(0.30)
    assert result.snapshot.features.birch_fraction == pytest.approx(0.20)
    assert result.snapshot.features.other_deciduous_fraction == pytest.approx(0.10)
    for name, raw_value in PROFILE_VALUES.items():
        assert result.snapshot.feature_provenance[name].raw_value == raw_value
        assert result.snapshot.feature_provenance[name].is_mock is False


def test_nodata_in_one_raster_keeps_other_layers_valid(tmp_path: Path) -> None:
    location = Location(59.412, 18.132)
    values = {**PROFILE_VALUES, "birch_fraction": -1}
    source = ForestProfileRasterDataSource(
        _profile_rasters(tmp_path, location, values),
        interpretation=ForestShareInterpretation.PROVISIONAL_ZERO_TO_HUNDRED_SHARE,
    )

    result = source.sample_profile(location)

    assert result.snapshot.features.spruce_fraction == pytest.approx(0.40)
    assert result.snapshot.features.birch_fraction is None
    assert result.diagnostics.complete is False
    assert result.diagnostics.status == "incomplete"
    assert result.diagnostics.tree_fraction_sum == pytest.approx(0.80)
    assert result.diagnostics.missing_features == ("birch_fraction",)
    assert result.diagnostics.nodata_features == ("birch_fraction",)


@pytest.mark.parametrize(
    ("values", "status", "near", "under", "over"),
    [
        ((0.4, 0.3, 0.2, 0.1), "near_one", True, False, False),
        ((0.3, 0.2, 0.2, 0.1), "clearly_below_one", False, True, False),
        ((0.5, 0.3, 0.2, 0.1), "exceeds_one", False, False, True),
    ],
)
def test_tree_fraction_sum_diagnostics(
    values: tuple[float, float, float, float],
    status: str,
    near: bool,
    under: bool,
    over: bool,
) -> None:
    features = StaticHabitatFeatures(
        spruce_fraction=values[0],
        pine_fraction=values[1],
        birch_fraction=values[2],
        other_deciduous_fraction=values[3],
    )
    diagnostics = diagnose_tree_fractions(features)

    assert diagnostics.tree_fraction_sum == pytest.approx(sum(values))
    assert diagnostics.status == status
    assert diagnostics.is_near_one is near
    assert diagnostics.is_clearly_below_one is under
    assert diagnostics.exceeds_one is over


def test_profile_hybrid_still_runs_existing_scoring_service(tmp_path: Path) -> None:
    location = Location(59.412, 18.132)
    real = ForestProfileRasterDataSource(
        _profile_rasters(tmp_path, location),
        interpretation=ForestShareInterpretation.PROVISIONAL_ZERO_TO_HUNDRED_SHARE,
    )
    hybrid = HybridHabitatDataSource(real, MockHabitatDataSource())

    snapshot = hybrid.get_features(location)
    assert snapshot.feature_provenance["spruce_fraction"].is_mock is False
    assert snapshot.feature_provenance["other_deciduous_fraction"].is_mock is False
    assert snapshot.features.beech_fraction is None
    assert snapshot.features.oak_fraction is None

    result = MushroomScoringService(hybrid, MockWeatherDataSource()).get_score(
        location.latitude,
        location.longitude,
        "2026-08-23",
        "cantharellus_cibarius",
    )
    assert 0 <= result.final_score <= 1
    assert result.data_sources["habitat"] == "hybrid_real_raster_and_mock_habitat_v0"
    assert result.feature_provenance["pine_fraction"].raw_value == 30

