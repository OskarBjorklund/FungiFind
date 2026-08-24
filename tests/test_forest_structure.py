from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin

from fungifind.data_sources import (
    CompositeHabitatDataSource,
    ConfiguredRasterFeatureDataSource,
    ForestProfileRasterDataSource,
    ForestShareInterpretation,
    HybridHabitatDataSource,
    MockHabitatDataSource,
    MockWeatherDataSource,
    RasterFeatureConfig,
    RasterPointReader,
)
from fungifind.models import Location
from fungifind.service import MushroomScoringService


def _write_point_raster(
    path: Path,
    location: Location,
    raw_value: int,
    *,
    crs: str = "EPSG:3006",
    pixel_size: float = 10,
) -> None:
    transformer = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    x, y = transformer.transform(location.longitude, location.latitude)
    transform = from_origin(x - 1.5 * pixel_size, y + 1.5 * pixel_size, pixel_size, pixel_size)
    data = np.zeros((4, 4), dtype=np.int16)
    data[1, 1] = raw_value
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


def _structure_source(
    tmp_path: Path,
    location: Location,
    values: tuple[int, int, int] = (123, 45, 17),
    crs_values: tuple[str, str, str] = ("EPSG:3006", "EPSG:3006", "EPSG:3006"),
    pixel_sizes: tuple[float, float, float] = (10, 10, 10),
) -> ConfiguredRasterFeatureDataSource:
    definitions = (
        ("forest_mean_height", "HGV"),
        ("vegetation_ratio", "Vegkvot"),
        ("basal_area", "GY"),
    )
    configs = []
    for index, (target, label) in enumerate(definitions):
        path = tmp_path / f"{label}.tif"
        _write_point_raster(
            path,
            location,
            values[index],
            crs=crs_values[index],
            pixel_size=pixel_sizes[index],
        )
        configs.append(RasterFeatureConfig(target, label, path))
    return ConfiguredRasterFeatureDataSource(configs)


def test_hgv_vegkvot_and_gy_mapping_preserves_raw_unvalidated_values(tmp_path: Path) -> None:
    location = Location(59.412, 18.132)
    source = _structure_source(tmp_path, location)

    assert all(isinstance(reader, RasterPointReader) for reader in source.readers.values())
    result = source.sample_features(location)
    expected = {
        "forest_mean_height": 123,
        "vegetation_ratio": 45,
        "basal_area": 17,
    }
    for name, raw_value in expected.items():
        item = result.snapshot.feature_provenance[name]
        assert item.raw_value == raw_value
        assert item.interpreted_value is None
        assert item.is_nodata is False
        assert item.semantic_status == "raw_value_preserved_semantics_unvalidated"
        assert item.source_path.endswith(".tif")
        assert item.grid_signature
    assert result.snapshot.features.forest_mean_height is None
    assert result.snapshot.features.vegetation_ratio is None
    assert result.snapshot.features.basal_area is None
    assert result.grid_alignment.status == "exact"


@pytest.mark.parametrize(
    ("values", "nodata_feature"),
    [
        ((-1, 45, 17), "forest_mean_height"),
        ((123, -1, 17), "vegetation_ratio"),
        ((123, 45, -1), "basal_area"),
    ],
)
def test_nodata_is_independent_per_structure_raster(
    tmp_path: Path,
    values: tuple[int, int, int],
    nodata_feature: str,
) -> None:
    location = Location(59.412, 18.132)
    result = _structure_source(tmp_path, location, values).sample_features(location)

    assert result.snapshot.feature_provenance[nodata_feature].is_nodata is True
    assert result.snapshot.feature_provenance[nodata_feature].semantic_status == "nodata"
    valid = [name for name in result.snapshot.feature_provenance if name != nodata_feature]
    assert all(not result.snapshot.feature_provenance[name].is_nodata for name in valid)


def test_different_crs_are_sampled_independently_and_report_different_grid(
    tmp_path: Path,
) -> None:
    location = Location(59.412, 18.132)
    source = _structure_source(
        tmp_path,
        location,
        crs_values=("EPSG:3006", "EPSG:3857", "EPSG:3006"),
    )
    result = source.sample_features(location)

    assert result.grid_alignment.status == "different"
    assert result.grid_alignment.distinct_grid_count == 2
    assert result.snapshot.feature_provenance["forest_mean_height"].raw_value == 123
    assert result.snapshot.feature_provenance["vegetation_ratio"].raw_value == 45
    assert result.snapshot.feature_provenance["vegetation_ratio"].details["source_epsg"] == 3857


def test_same_crs_with_different_pixel_size_is_not_exactly_aligned(tmp_path: Path) -> None:
    location = Location(59.412, 18.132)
    result = _structure_source(
        tmp_path,
        location,
        pixel_sizes=(10, 20, 10),
    ).sample_features(location)

    assert result.grid_alignment.status == "different"
    assert result.grid_alignment.distinct_grid_count == 2


def test_unscored_structure_provenance_does_not_change_scoring(tmp_path: Path) -> None:
    location = Location(59.412, 18.132)
    baseline = MushroomScoringService(MockHabitatDataSource(), MockWeatherDataSource()).get_score(
        location.latitude,
        location.longitude,
        "2026-08-23",
        "cantharellus_cibarius",
    )
    structure = _structure_source(tmp_path, location)
    hybrid = HybridHabitatDataSource(structure, MockHabitatDataSource())
    with_structure = MushroomScoringService(hybrid, MockWeatherDataSource()).get_score(
        location.latitude,
        location.longitude,
        "2026-08-23",
        "cantharellus_cibarius",
    )

    assert with_structure.habitat_score == baseline.habitat_score
    assert with_structure.fruiting_score == baseline.fruiting_score
    assert with_structure.final_score == baseline.final_score
    assert with_structure.confidence == baseline.confidence
    assert with_structure.feature_provenance["forest_mean_height"].raw_value == 123
    assert with_structure.feature_provenance["vegetation_ratio"].raw_value == 45
    assert with_structure.feature_provenance["basal_area"].raw_value == 17


def test_tree_profile_plus_structure_runs_service_without_score_change(tmp_path: Path) -> None:
    location = Location(59.412, 18.132)
    tree_values = {
        "spruce_fraction": 40,
        "pine_fraction": 30,
        "birch_fraction": 20,
        "other_deciduous_fraction": 10,
    }
    tree_paths: dict[str, Path] = {}
    for name, raw_value in tree_values.items():
        path = tmp_path / f"tree_{name}.tif"
        _write_point_raster(path, location, raw_value)
        tree_paths[name] = path
    trees = ForestProfileRasterDataSource(
        tree_paths,
        interpretation=ForestShareInterpretation.PROVISIONAL_ZERO_TO_HUNDRED_SHARE,
    )
    structure = _structure_source(tmp_path, location)
    fallback = MockHabitatDataSource()
    weather = MockWeatherDataSource()

    tree_only = MushroomScoringService(
        HybridHabitatDataSource(trees, fallback), weather
    ).get_score(
        location.latitude,
        location.longitude,
        "2026-08-23",
        "cantharellus_cibarius",
    )
    combined_real = CompositeHabitatDataSource(
        {"tree_species": trees, "forest_structure": structure}
    )
    combined = MushroomScoringService(
        HybridHabitatDataSource(combined_real, fallback), weather
    ).get_score(
        location.latitude,
        location.longitude,
        "2026-08-23",
        "cantharellus_cibarius",
    )

    assert combined.habitat_score == tree_only.habitat_score
    assert combined.fruiting_score == tree_only.fruiting_score
    assert combined.final_score == tree_only.final_score
    assert combined.confidence == tree_only.confidence
    assert combined.feature_provenance["spruce_fraction"].raw_value == 40
    assert combined.feature_provenance["forest_mean_height"].raw_value == 123
