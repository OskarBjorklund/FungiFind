from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import date
from pathlib import Path

from shapely.geometry import MultiPolygon, box
from shapely.geometry.base import BaseGeometry

from fungifind.data_sources.forest_raster import HybridHabitatDataSource
from fungifind.data_sources.mock import MockHabitatDataSource, MockWeatherDataSource
from fungifind.data_sources.soil_vector import (
    SGU_GRUNDLAGER_CLASS_LABELS,
    SGU_SOIL_GROUP_BY_CODE,
    SguSoilClassMapping,
    SguSoilVectorDataSource,
)
from fungifind.models import FeatureProvenance, FeatureSnapshot, Location, Species
from fungifind.scoring import RuleBasedScoringEngine
from fungifind.service import MushroomScoringService

GpkgFactory = Callable[..., Path]


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
            "kartering": "synthetic_mapping_area",
            "karttyp": 2,
            "symbol": 17,
            "objectid": 1234,
            "geom_area": geometry.area,
            "geom_length": geometry.length,
        },
    )


def test_official_mapping_and_derived_groups_are_explicit_and_complete() -> None:
    assert set(SGU_GRUNDLAGER_CLASS_LABELS) == set(SGU_SOIL_GROUP_BY_CODE)
    assert SGU_GRUNDLAGER_CLASS_LABELS[100] == "Morän"
    assert SGU_SOIL_GROUP_BY_CODE[100] == "moraine"
    assert SGU_SOIL_GROUP_BY_CODE[75] == "organic_peat"
    assert SGU_SOIL_GROUP_BY_CODE[40] == "fine_mineral"
    assert SGU_SOIL_GROUP_BY_CODE[57] == "coarse_mineral"
    assert SGU_SOIL_GROUP_BY_CODE[888] == "bedrock_or_thin_soil"


def test_validated_soil_mapping_populates_static_fields_and_provenance(
    synthetic_gpkg_factory: GpkgFactory,
) -> None:
    path = synthetic_gpkg_factory(
        epsg=4326,
        features=(_feature(42, box(17.0, 58.0, 19.0, 60.0), 100, "Morän"),),
    )
    result = SguSoilVectorDataSource(
        path, class_mapping=SguSoilClassMapping.official()
    ).sample_soil(Location(59.0, 18.0))

    features = result.snapshot.features
    provenance = result.snapshot.feature_provenance["soil_type_code"]
    assert features.soil_type_code == 100
    assert features.soil_type_label == "Morän"
    assert features.soil_group == "moraine"
    assert features.soil_type is None
    assert provenance.raw_value == 100
    assert provenance.interpreted_value == 100
    assert provenance.semantic_status.startswith("validated")
    assert provenance.details["layer"] == "grundlager"
    assert provenance.details["feature_id"] == 42
    assert provenance.details["source_epsg"] == 4326
    assert provenance.details["lookup_method"].startswith("gpkg_rtree")
    assert '"jg2_tx": "Morän"' in provenance.details["raw_attributes_json"]
    assert (
        result.snapshot.feature_provenance["soil_group"].semantic_status
        == "derived_from_validated_official_sgu_class_mapping"
    )


def test_unknown_or_mismatched_class_is_preserved_but_not_interpreted(
    synthetic_gpkg_factory: GpkgFactory,
) -> None:
    unknown_path = synthetic_gpkg_factory(
        epsg=4326,
        features=(_feature(1, box(0.0, 0.0, 1.0, 1.0), 999999, "Okänd"),),
        filename="unknown.gpkg",
    )
    mismatch_path = synthetic_gpkg_factory(
        epsg=4326,
        features=(_feature(2, box(0.0, 0.0, 1.0, 1.0), 100, "Inte morän"),),
        filename="mismatch.gpkg",
    )

    unknown = SguSoilVectorDataSource(
        unknown_path, class_mapping=SguSoilClassMapping.official()
    ).sample_soil(Location(0.5, 0.5))
    mismatch = SguSoilVectorDataSource(
        mismatch_path, class_mapping=SguSoilClassMapping.official()
    ).sample_soil(Location(0.5, 0.5))

    assert unknown.snapshot.features.soil_type_code is None
    assert unknown.snapshot.feature_provenance["soil_type_code"].raw_value == 999999
    assert (
        unknown.snapshot.feature_provenance["soil_type_code"].semantic_status
        == "unknown_class_not_in_validated_mapping"
    )
    assert mismatch.snapshot.features.soil_type_code is None
    assert (
        mismatch.snapshot.feature_provenance["soil_type_code"].semantic_status
        == "official_code_label_mismatch"
    )


def test_unvalidated_semantics_and_no_polygon_leave_domain_fields_empty(
    synthetic_gpkg_factory: GpkgFactory,
) -> None:
    path = synthetic_gpkg_factory(
        epsg=4326,
        features=(_feature(1, box(0.0, 0.0, 1.0, 1.0), 75, "Torv"),),
    )
    source = SguSoilVectorDataSource(path, class_mapping=None)

    unvalidated = source.sample_soil(Location(0.5, 0.5))
    outside = source.sample_soil(Location(2.0, 2.0))

    assert unvalidated.snapshot.features.soil_type_code is None
    assert (
        unvalidated.snapshot.feature_provenance["soil_type_code"].semantic_status
        == "raw_sgu_class_preserved_semantics_unvalidated"
    )
    assert outside.sample.found is False
    assert outside.snapshot.features.soil_type_code is None
    assert (
        outside.snapshot.feature_provenance["soil_type_code"].semantic_status
        == "no_feature_at_location"
    )


def test_new_sgu_fields_do_not_change_existing_scoring() -> None:
    location = Location(59.412, 18.132)
    target_date = date(2026, 8, 23)
    habitat = MockHabitatDataSource().get_features(location)
    weather = MockWeatherDataSource().get_features(location, target_date)
    engine = RuleBasedScoringEngine()
    baseline = engine.score(
        Species.CANTHARELLUS_CIBARIUS, location, target_date, habitat, weather
    )

    soil_provenance = FeatureProvenance(
        source_name="sgu_jordarter_25k_100k",
        quality=0.98,
        is_mock=False,
        semantic_status="validated_official_sgu_jordarter_25k_100k_mapping",
        raw_value=100,
        interpreted_value=100,
        details={"interpreted_soil_group": "moraine"},
    )
    with_soil = FeatureSnapshot(
        features=replace(
            habitat.features,
            soil_type_code=100,
            soil_type_label="Morän",
            soil_group="moraine",
        ),
        metadata=habitat.metadata,
        feature_provenance={
            **habitat.feature_provenance,
            "soil_type_code": soil_provenance,
            "soil_type_label": soil_provenance,
            "soil_group": soil_provenance,
        },
    )
    actual = engine.score(
        Species.CANTHARELLUS_CIBARIUS,
        location,
        target_date,
        with_soil,
        weather,
    )

    assert actual.habitat_score == baseline.habitat_score
    assert actual.fruiting_score == baseline.fruiting_score
    assert actual.final_score == baseline.final_score
    assert actual.confidence == baseline.confidence
    assert actual.factors == baseline.factors
    assert actual.missing_features == baseline.missing_features


def test_sgu_adapter_integrates_through_service_without_changing_score(
    synthetic_gpkg_factory: GpkgFactory,
) -> None:
    location = Location(59.412, 18.132)
    path = synthetic_gpkg_factory(
        epsg=4326,
        features=(_feature(7, box(18.0, 59.0, 18.5, 59.8), 100, "Morän"),),
    )
    fallback = MockHabitatDataSource()
    weather = MockWeatherDataSource()
    baseline_service = MushroomScoringService(fallback, weather)
    soil_source = SguSoilVectorDataSource(
        path, class_mapping=SguSoilClassMapping.official()
    )
    integrated_service = MushroomScoringService(
        HybridHabitatDataSource(soil_source, fallback), weather
    )

    baseline = baseline_service.get_score(
        location.latitude,
        location.longitude,
        "2026-08-23",
        Species.CANTHARELLUS_CIBARIUS,
    )
    actual = integrated_service.get_score(
        location.latitude,
        location.longitude,
        "2026-08-23",
        Species.CANTHARELLUS_CIBARIUS,
    )

    assert actual.habitat_score == baseline.habitat_score
    assert actual.fruiting_score == baseline.fruiting_score
    assert actual.final_score == baseline.final_score
    assert actual.confidence == baseline.confidence
    assert actual.feature_provenance["soil_type_code"].raw_value == 100
    assert actual.feature_provenance["soil_group"].semantic_status.startswith("derived")
