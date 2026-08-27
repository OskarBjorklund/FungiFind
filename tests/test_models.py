from __future__ import annotations

import pytest

from fungifind.models import DynamicWeatherFeatures, Location, Species, StaticHabitatFeatures


def test_location_validates_wgs84_bounds() -> None:
    with pytest.raises(ValueError, match="latitude"):
        Location(latitude=91.0, longitude=18.0)


def test_fraction_features_are_validated() -> None:
    with pytest.raises(ValueError, match="forest_cover_fraction"):
        StaticHabitatFeatures(forest_cover_fraction=1.2)
    with pytest.raises(ValueError, match="estimated_current_soil_moisture_index"):
        DynamicWeatherFeatures(estimated_current_soil_moisture_index=-0.1)


def test_terrain_domain_allows_below_sea_level_but_normalizes_aspect_range() -> None:
    assert StaticHabitatFeatures(elevation_m=-0.25).elevation_m == -0.25
    with pytest.raises(ValueError, match="below 360"):
        StaticHabitatFeatures(aspect_degrees=360.0)


def test_landcover_class_is_an_explicit_integer_category() -> None:
    features = StaticHabitatFeatures(landcover_class=113, landcover_label="Barrblandskog")
    assert features.landcover_class == 113
    assert features.landcover_label == "Barrblandskog"
    with pytest.raises(ValueError, match="landcover_class"):
        StaticHabitatFeatures(landcover_class=3.5)  # type: ignore[arg-type]


def test_sgu_soil_fields_are_separate_from_the_legacy_scoring_soil_type() -> None:
    features = StaticHabitatFeatures(
        soil_type_code=100,
        soil_type_label="Morän",
        soil_group="moraine",
    )
    assert features.soil_type_code == 100
    assert features.soil_type_label == "Morän"
    assert features.soil_group == "moraine"
    assert features.soil_type is None
    with pytest.raises(ValueError, match="soil_type_code"):
        StaticHabitatFeatures(soil_type_code=100.5)  # type: ignore[arg-type]


def test_species_parser_rejects_unknown_species() -> None:
    with pytest.raises(ValueError, match="Unsupported species"):
        Species.parse("boletus_edulis")
