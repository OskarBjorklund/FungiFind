from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin, xy

from fungifind.data_sources import (
    NMD2023_V2_1_CLASS_LABELS,
    NMD2023_V2_1_EXCLUSION_RULES,
    CompositeHabitatDataSource,
    HybridHabitatDataSource,
    MockHabitatDataSource,
    MockWeatherDataSource,
    NmdLandcoverClassMapping,
    NmdLandcoverRasterDataSource,
    RasterPointReader,
    StaticWetnessClassMapping,
    StaticWetnessRasterDataSource,
)
from fungifind.models import Location, Species
from fungifind.service import MushroomScoringService


def _mapping() -> NmdLandcoverClassMapping:
    return NmdLandcoverClassMapping(
        labels=NMD2023_V2_1_CLASS_LABELS,
        exclusion_rules=NMD2023_V2_1_EXCLUSION_RULES,
        source_reference="synthetic-test-copy-of-official-mapping",
    )


def _write_class_raster(
    path: Path,
    location: Location,
    *,
    class_value: int,
    crs: str = "EPSG:3006",
    pixel_size: float = 10.0,
) -> Location:
    to_raster = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    x, y = to_raster.transform(location.longitude, location.latitude)
    transform = from_origin(x - 1.5 * pixel_size, y + 1.5 * pixel_size, pixel_size, pixel_size)
    values = np.full((4, 4), 111, dtype=np.uint16)
    values[1, 1] = class_value
    values[2, 2] = 65535
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=4,
        height=4,
        count=1,
        dtype="uint16",
        crs=crs,
        transform=transform,
        nodata=65535,
    ) as dataset:
        dataset.write(values, 1)

    nodata_x, nodata_y = xy(transform, 2, 2)
    to_wgs84 = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    nodata_lon, nodata_lat = to_wgs84.transform(nodata_x, nodata_y)
    return Location(nodata_lat, nodata_lon)


def _score(source: NmdLandcoverRasterDataSource, location: Location):
    habitat = HybridHabitatDataSource(source, MockHabitatDataSource())
    return MushroomScoringService(habitat, MockWeatherDataSource()).get_score(
        location.latitude,
        location.longitude,
        "2026-08-23",
        Species.CANTHARELLUS_CIBARIUS,
    )


def test_categorical_mapping_and_provenance(tmp_path: Path) -> None:
    location = Location(59.412, 18.132)
    path = tmp_path / "nmd_forest.tif"
    _write_class_raster(path, location, class_value=112)
    source = NmdLandcoverRasterDataSource(path, class_mapping=_mapping())

    assert isinstance(source.reader, RasterPointReader)
    result = source.sample_landcover(location)
    provenance = result.snapshot.feature_provenance["landcover_class"]

    assert result.snapshot.features.landcover_class == 112
    assert result.snapshot.features.landcover_label == "Granskog på fastmark"
    assert provenance.raw_value == 112
    assert provenance.interpreted_value == 112
    assert provenance.semantic_status == "validated_official_nmd_class_mapping"
    assert provenance.source_path == str(path.resolve())
    assert provenance.details["source_file"] == path.name
    assert provenance.details["source_epsg"] == 3006
    assert provenance.details["pixel_row"] == 1
    assert provenance.details["pixel_col"] == 1
    assert provenance.details["nodata_value"] == 65535
    assert provenance.details["searchable_habitat"] == "yes"


def test_nodata_unknown_and_unvalidated_semantics(tmp_path: Path) -> None:
    location = Location(59.412, 18.132)
    path = tmp_path / "nmd_unknown.tif"
    nodata_location = _write_class_raster(path, location, class_value=999)

    validated = NmdLandcoverRasterDataSource(path, class_mapping=_mapping())
    unknown = validated.sample_landcover(location)
    unknown_provenance = unknown.snapshot.feature_provenance["landcover_class"]
    assert unknown.snapshot.features.landcover_class is None
    assert unknown.snapshot.features.landcover_label is None
    assert unknown_provenance.raw_value == 999
    assert unknown_provenance.semantic_status == "unknown_class_not_in_validated_mapping"
    assert unknown_provenance.details["searchable_habitat"] == "unknown"

    nodata = validated.sample_landcover(nodata_location)
    nodata_provenance = nodata.snapshot.feature_provenance["landcover_class"]
    assert nodata.snapshot.features.landcover_class is None
    assert nodata_provenance.raw_value == 65535
    assert nodata_provenance.is_nodata is True
    assert nodata_provenance.semantic_status == "nodata"

    unvalidated = NmdLandcoverRasterDataSource(path).sample_landcover(location)
    unvalidated_provenance = unvalidated.snapshot.feature_provenance["landcover_class"]
    assert unvalidated.snapshot.features.landcover_class is None
    assert unvalidated_provenance.raw_value == 999
    assert "unvalidated" in unvalidated_provenance.semantic_status


@pytest.mark.parametrize(
    ("raw_class", "expected_reason"),
    [
        (61, "open_water"),
        (51, "built_or_artificial_land"),
        (52, "built_or_artificial_land"),
        (3, "agricultural_land"),
        (412, "permanent_ice_or_snow"),
    ],
)
def test_safe_official_classes_are_excluded(
    tmp_path: Path, raw_class: int, expected_reason: str
) -> None:
    location = Location(59.412, 18.132)
    path = tmp_path / f"nmd_{raw_class}.tif"
    _write_class_raster(path, location, class_value=raw_class)

    result = _score(NmdLandcoverRasterDataSource(path, class_mapping=_mapping()), location)

    assert result.eligibility_status == "excluded"
    assert result.score_type == "excluded_habitat_no_suitability_index_v0"
    assert result.habitat_score is None
    assert result.fruiting_score is None
    assert result.final_score is None
    assert result.factors == {}
    assert len(result.habitat_exclusions) == 1
    exclusion = result.habitat_exclusions[0]
    assert exclusion.reason_code == expected_reason
    assert exclusion.source_feature == "landcover_class"
    assert exclusion.raw_value == raw_class
    assert exclusion.source_path == str(path.resolve())


@pytest.mark.parametrize("raw_class", [111, 121, 211, 4233])
def test_forest_wetland_and_natural_land_are_not_excluded_and_score_is_unchanged(
    tmp_path: Path, raw_class: int
) -> None:
    location = Location(59.412, 18.132)
    path = tmp_path / f"nmd_valid_{raw_class}.tif"
    _write_class_raster(path, location, class_value=raw_class)
    baseline = MushroomScoringService(MockHabitatDataSource(), MockWeatherDataSource()).get_score(
        location.latitude,
        location.longitude,
        "2026-08-23",
        Species.CANTHARELLUS_CIBARIUS,
    )

    result = _score(NmdLandcoverRasterDataSource(path, class_mapping=_mapping()), location)

    assert result.eligibility_status == "eligible"
    assert result.habitat_exclusions == ()
    assert result.habitat_score == baseline.habitat_score
    assert result.fruiting_score == baseline.fruiting_score
    assert result.final_score == baseline.final_score
    assert result.confidence == baseline.confidence


def test_multiple_exclusion_sources_are_all_preserved(tmp_path: Path) -> None:
    location = Location(59.412, 18.132)
    landcover_path = tmp_path / "nmd_agriculture.tif"
    wetness_path = tmp_path / "wetness_water.tif"
    _write_class_raster(landcover_path, location, class_value=3)
    _write_class_raster(wetness_path, location, class_value=4)

    landcover = NmdLandcoverRasterDataSource(landcover_path, class_mapping=_mapping())
    wetness = StaticWetnessRasterDataSource(
        wetness_path,
        class_mapping=StaticWetnessClassMapping(
            labels={1: "dry", 2: "mesic", 3: "wet", 4: "open water"},
            source_reference="synthetic-test-mapping",
        ),
    )
    composite = CompositeHabitatDataSource({"landcover": landcover, "wetness": wetness})
    habitat = HybridHabitatDataSource(composite, MockHabitatDataSource())

    result = MushroomScoringService(habitat, MockWeatherDataSource()).get_score(
        location.latitude,
        location.longitude,
        "2026-08-23",
        Species.CANTHARELLUS_CIBARIUS,
    )

    assert result.eligibility_status == "excluded"
    assert result.final_score is None
    assert {item.reason_code for item in result.habitat_exclusions} == {
        "agricultural_land",
        "open_water",
    }
    assert {item.source_feature for item in result.habitat_exclusions} == {
        "landcover_class",
        "static_wetness_class",
    }
    assert len(result.habitat_exclusions) == 2


def test_unvalidated_exclusion_marker_is_ignored(tmp_path: Path) -> None:
    location = Location(59.412, 18.132)
    path = tmp_path / "nmd_unvalidated_water.tif"
    _write_class_raster(path, location, class_value=61)
    baseline = MushroomScoringService(MockHabitatDataSource(), MockWeatherDataSource()).get_score(
        location.latitude,
        location.longitude,
        "2026-08-23",
        Species.CANTHARELLUS_CIBARIUS,
    )

    result = _score(NmdLandcoverRasterDataSource(path), location)

    assert result.eligibility_status == "eligible"
    assert result.final_score == baseline.final_score
    assert result.habitat_exclusions == ()
