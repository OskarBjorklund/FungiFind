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


def test_species_parser_rejects_unknown_species() -> None:
    with pytest.raises(ValueError, match="Unsupported species"):
        Species.parse("boletus_edulis")
