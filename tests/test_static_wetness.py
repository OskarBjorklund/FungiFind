from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import numpy as np
import pytest
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin, xy

from fungifind.config import get_species_config
from fungifind.data_sources import (
    HybridHabitatDataSource,
    MockHabitatDataSource,
    MockWeatherDataSource,
    RasterPointReader,
    StaticWetnessClassMapping,
    StaticWetnessRasterDataSource,
    diagnose_grid_alignment,
)
from fungifind.data_sources.raster import RasterPointOutsideBoundsError
from fungifind.models import DynamicWeatherFeatures, Location, Species, StaticHabitatFeatures
from fungifind.service import MushroomScoringService


def _write_class_raster(
    path: Path,
    location: Location,
    *,
    class_value: int = 2,
    crs: str = "EPSG:3006",
    pixel_size: float = 10.0,
) -> Location:
    to_raster = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    x, y = to_raster.transform(location.longitude, location.latitude)
    transform = from_origin(
        x - 1.5 * pixel_size,
        y + 1.5 * pixel_size,
        pixel_size,
        pixel_size,
    )
    values = np.ones((4, 4), dtype=np.uint8)
    values[1, 1] = class_value
    values[2, 2] = 255
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=4,
        height=4,
        count=1,
        dtype="uint8",
        crs=crs,
        transform=transform,
        nodata=255,
    ) as dataset:
        dataset.write(values, 1)

    nodata_x, nodata_y = xy(transform, 2, 2)
    to_wgs84 = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    nodata_lon, nodata_lat = to_wgs84.transform(nodata_x, nodata_y)
    return Location(nodata_lat, nodata_lon)


def _validated_mapping() -> StaticWetnessClassMapping:
    return StaticWetnessClassMapping(
        labels={1: "dry", 2: "mesic", 3: "wet", 4: "open water"},
        source_reference="synthetic-test-mapping",
    )


def test_validated_integer_class_mapping_and_provenance(tmp_path: Path) -> None:
    location = Location(59.412, 18.132)
    path = tmp_path / "wetness_3006.tif"
    _write_class_raster(path, location, class_value=2)
    source = StaticWetnessRasterDataSource(path, class_mapping=_validated_mapping())

    assert isinstance(source.reader, RasterPointReader)
    result = source.sample_wetness(location)
    provenance = result.snapshot.feature_provenance["static_wetness_class"]

    assert result.snapshot.features.static_wetness_class == 2
    assert result.snapshot.features.static_wetness_label == "mesic"
    assert provenance.raw_value == 2
    assert provenance.interpreted_value == 2
    assert provenance.semantic_status == "validated_class_mapping"
    assert provenance.is_nodata is False
    assert provenance.source_path == str(path.resolve())
    assert provenance.details["source_epsg"] == 3006
    assert provenance.details["pixel_row"] == 1
    assert provenance.details["pixel_col"] == 1
    assert "long_term_static" in provenance.details["temporal_meaning"]


def test_unvalidated_class_preserves_raw_and_is_not_a_model_value(tmp_path: Path) -> None:
    location = Location(59.412, 18.132)
    path = tmp_path / "unvalidated.tif"
    _write_class_raster(path, location, class_value=3)

    result = StaticWetnessRasterDataSource(path).sample_wetness(location)
    provenance = result.snapshot.feature_provenance["static_wetness_class"]

    assert result.snapshot.features.static_wetness_class is None
    assert result.snapshot.features.static_wetness_label is None
    assert provenance.raw_value == 3
    assert provenance.interpreted_value is None
    assert "unvalidated" in provenance.semantic_status


def test_nodata_and_outside_bounds_are_handled(tmp_path: Path) -> None:
    location = Location(59.412, 18.132)
    path = tmp_path / "wetness_nodata.tif"
    nodata_location = _write_class_raster(path, location)
    source = StaticWetnessRasterDataSource(path, class_mapping=_validated_mapping())

    nodata = source.sample_wetness(nodata_location)
    provenance = nodata.snapshot.feature_provenance["static_wetness_class"]
    assert nodata.snapshot.features.static_wetness_class is None
    assert provenance.raw_value == 255
    assert provenance.is_nodata is True
    assert provenance.semantic_status == "nodata"

    with pytest.raises(RasterPointOutsideBoundsError, match="outside raster bounds"):
        source.sample_wetness(Location(0.0, 0.0))


def test_each_crs_and_grid_is_sampled_independently(tmp_path: Path) -> None:
    location = Location(59.412, 18.132)
    first_path = tmp_path / "wetness_3006.tif"
    second_path = tmp_path / "wetness_3857.tif"
    _write_class_raster(first_path, location, class_value=1, crs="EPSG:3006")
    _write_class_raster(second_path, location, class_value=3, crs="EPSG:3857", pixel_size=20)

    first = StaticWetnessRasterDataSource(
        first_path, class_mapping=_validated_mapping()
    ).sample_wetness(location)
    second = StaticWetnessRasterDataSource(
        second_path, class_mapping=_validated_mapping()
    ).sample_wetness(location)
    alignment = diagnose_grid_alignment(
        {
            "first": first.snapshot.feature_provenance["static_wetness_class"],
            "second": second.snapshot.feature_provenance["static_wetness_class"],
        }
    )

    assert first.snapshot.features.static_wetness_class == 1
    assert second.snapshot.features.static_wetness_class == 3
    assert first.sample.source_epsg == 3006
    assert second.sample.source_epsg == 3857
    assert alignment.status == "different"


def test_static_wetness_and_dynamic_current_moisture_are_distinct_fields() -> None:
    static_names = {item.name for item in fields(StaticHabitatFeatures)}
    dynamic_names = {item.name for item in fields(DynamicWeatherFeatures)}

    assert "static_wetness_class" in static_names
    assert "static_wetness_label" in static_names
    assert "estimated_current_soil_moisture_index" in dynamic_names
    assert "static_wetness_class" not in dynamic_names
    assert "estimated_current_soil_moisture_index" not in static_names


def test_unvalidated_semantics_leave_all_scores_unchanged(tmp_path: Path) -> None:
    location = Location(59.412, 18.132)
    path = tmp_path / "unvalidated_score.tif"
    _write_class_raster(path, location, class_value=2)
    weather = MockWeatherDataSource()
    baseline = MushroomScoringService(MockHabitatDataSource(), weather).get_score(
        location.latitude,
        location.longitude,
        "2026-08-23",
        Species.CANTHARELLUS_CIBARIUS,
    )
    unvalidated = HybridHabitatDataSource(
        StaticWetnessRasterDataSource(path),
        MockHabitatDataSource(),
    )
    result = MushroomScoringService(unvalidated, weather).get_score(
        location.latitude,
        location.longitude,
        "2026-08-23",
        Species.CANTHARELLUS_CIBARIUS,
    )

    assert result.habitat_score == baseline.habitat_score
    assert result.fruiting_score == baseline.fruiting_score
    assert result.final_score == baseline.final_score
    assert result.confidence == baseline.confidence
    assert "static_wetness" not in result.factors
    assert result.feature_provenance["static_wetness_class"].raw_value == 2


def test_validated_mapping_activates_species_specific_static_wetness_score(
    tmp_path: Path,
) -> None:
    location = Location(59.412, 18.132)
    path = tmp_path / "validated_score.tif"
    _write_class_raster(path, location, class_value=3)
    source = HybridHabitatDataSource(
        StaticWetnessRasterDataSource(path, class_mapping=_validated_mapping()),
        MockHabitatDataSource(),
    )
    weather = MockWeatherDataSource()

    for species in Species:
        baseline = MushroomScoringService(MockHabitatDataSource(), weather).get_score(
            location.latitude, location.longitude, "2026-08-23", species
        )
        result = MushroomScoringService(source, weather).get_score(
            location.latitude, location.longitude, "2026-08-23", species
        )
        expected = get_species_config(species).static_wetness_preferences[3]
        assert result.factors["static_wetness"] == expected
        habitat_weights = get_species_config(species).habitat_component_weights
        expected_habitat = sum(
            habitat_weights[name] * result.factors[name] for name in habitat_weights
        ) / sum(habitat_weights.values())
        assert result.habitat_score == pytest.approx(expected_habitat, abs=2e-6)
        assert result.habitat_score != baseline.habitat_score
        assert result.final_score != baseline.final_score
