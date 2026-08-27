from __future__ import annotations

from datetime import date

import pytest

from fungifind.config import get_species_config
from fungifind.models import (
    DataSourceMetadata,
    DynamicWeatherFeatures,
    FeatureSnapshot,
    Location,
    SoilType,
    Species,
    StaticHabitatFeatures,
)
from fungifind.service import MushroomScoringService, get_mushroom_score


def test_complete_mock_pipeline_returns_suitability_index() -> None:
    result = get_mushroom_score(
        latitude=59.412,
        longitude=18.132,
        date="2026-08-23",
        species="cantharellus_cibarius",
    )

    assert result.species is Species.CANTHARELLUS_CIBARIUS
    assert 0 <= result.habitat_score <= 1
    assert 0 <= result.fruiting_score <= 1
    assert 0 <= result.final_score <= 1
    assert 0 < result.confidence < 0.5  # mock quality deliberately caps confidence
    assert result.score_type == "heuristic_suitability_index_v0"
    assert result.data_sources["habitat"].startswith("synthetic_")
    assert result.to_dict()["date"] == "2026-08-23"


def test_final_score_uses_species_configured_domain_weights() -> None:
    result = get_mushroom_score(
        59.412, 18.132, date(2026, 8, 23), Species.CANTHARELLUS_CIBARIUS
    )
    config = get_species_config(result.species)
    expected = (
        config.final_habitat_weight * result.habitat_score
        + (1 - config.final_habitat_weight) * result.fruiting_score
    )
    assert result.final_score == pytest.approx(expected, abs=1e-6)


def test_species_have_separate_parameters_and_scores() -> None:
    chanterelle = get_mushroom_score(
        59.412, 18.132, "2026-10-20", Species.CANTHARELLUS_CIBARIUS
    )
    funnel = get_mushroom_score(
        59.412, 18.132, "2026-10-20", Species.CRATERELLUS_TUBAEFORMIS
    )
    assert chanterelle.final_score != funnel.final_score
    assert chanterelle.factors["season"] != funnel.factors["season"]


class _SparseHabitatSource:
    def get_features(self, location: Location) -> FeatureSnapshot[StaticHabitatFeatures]:
        return FeatureSnapshot(
            StaticHabitatFeatures(
                forest_cover_fraction=0.8,
                spruce_fraction=0.5,
                soil_type=SoilType.TILL,
            ),
            DataSourceMetadata("sparse_habitat", quality=1.0),
        )


class _SparseWeatherSource:
    def get_features(
        self, location: Location, target_date: date
    ) -> FeatureSnapshot[DynamicWeatherFeatures]:
        return FeatureSnapshot(
            DynamicWeatherFeatures(rainfall_14d_mm=45.0),
            DataSourceMetadata("sparse_weather", quality=1.0),
        )


def test_missing_features_are_reported_and_reduce_confidence() -> None:
    service = MushroomScoringService(_SparseHabitatSource(), _SparseWeatherSource())
    result = service.get_score(59.4, 18.1, "2026-08-23", "cantharellus_cibarius")

    assert "canopy_density_fraction" in result.missing_features
    assert "temp_mean_7d_c" in result.missing_features
    assert result.confidence < 0.5
    assert 0 <= result.final_score <= 1

