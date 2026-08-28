from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from fungifind.api.app import create_app, get_scoring_service
from fungifind.api.schemas import ScoreResponse
from fungifind.data_sources import MesanHistoryError
from fungifind.models import (
    CurrentSoilMoistureResult,
    FeatureProvenance,
    FruitingV2Breakdown,
    HabitatExclusion,
    Location,
    ModelResult,
    MoistureInputUsage,
    Species,
)


def _provenance(
    value: float | None,
    *,
    source_name: str = "official_test_source",
    details: dict[str, str | float | int] | None = None,
) -> FeatureProvenance:
    return FeatureProvenance(
        source_name=source_name,
        quality=0.94,
        is_mock=False,
        semantic_status="validated_test_value",
        source_path=r"C:\private\model-data\source.tif",
        raw_value=value,
        interpreted_value=value,
        details=details or {},
    )


def _eligible_result(
    *,
    target_date: date = date(2026, 8, 27),
    moisture_status: str = "estimated_complete",
) -> ModelResult:
    moisture = CurrentSoilMoistureResult(
        baseline_wetness=0.5,
        rain_recharge=0.7,
        drying_pressure=0.2,
        soil_retention=0.6,
        terrain_retention=0.55,
        estimated_current_soil_moisture=(
            None if moisture_status == "insufficient_central_inputs" else 0.64
        ),
        confidence=0.91,
        completeness=0.96,
        status=moisture_status,
        used_inputs=MoistureInputUsage(
            used_static_wetness=True,
            used_rain_history=True,
            used_temperature=True,
            used_relative_humidity=True,
            used_soil=True,
            used_slope=True,
        ),
        missing_inputs=(
            ("rainfall_30d_mm",)
            if moisture_status == "insufficient_central_inputs"
            else ()
        ),
    )
    v2 = FruitingV2Breakdown(
        status="experimental_complete",
        current_soil_moisture=0.64,
        moisture_preference_score=0.82,
        recent_rain_trigger=0.74,
        temperature_suitability=0.86,
        season_suitability=0.78,
        fruiting_score_v2=0.79,
        final_score_v2=0.76,
        moisture_confidence=0.91,
        moisture_completeness=0.96,
    )
    provenance = {
        "landcover_class": _provenance(
            111,
            details={"official_class_label": "Tallskog utanför våtmark"},
        ),
        "static_wetness_class": _provenance(
            2,
            details={"interpreted_class_label": "Frisk mark"},
        ),
        "soil_group": _provenance(
            None,
            details={
                "interpreted_soil_group": "moraine",
                "raw_official_label": "Sandig morän",
            },
        ),
        "spruce_fraction": _provenance(0.54),
        "pine_fraction": _provenance(0.29),
        "birch_fraction": _provenance(0.13),
        "other_deciduous_fraction": _provenance(0.04),
        "slope_degrees": _provenance(4.8),
        "rainfall_7d_mm": _provenance(
            24.0,
            details={
                "coverage_status": "complete",
                "coverage_fraction": 1.0,
                "available_hours": 168,
                "expected_hours": 168,
                "unit": "mm",
            },
        ),
        "rainfall_30d_mm": _provenance(78.0),
        "temp_mean_7d_c": _provenance(15.2),
    }
    return ModelResult(
        species=Species.CANTHARELLUS_CIBARIUS,
        location=Location(59.160136, 18.247348),
        date=target_date,
        habitat_score=0.81,
        fruiting_score=0.67,
        final_score=0.75,
        confidence=0.93,
        factors={"production_factor": 0.42},
        missing_features=(),
        data_sources={"habitat": "official_test_source"},
        feature_provenance=provenance,
        weather_completeness={"rainfall_7d_mm": "complete"},
        estimated_current_soil_moisture=moisture.estimated_current_soil_moisture,
        moisture_breakdown=moisture,
        fruiting_score_v2=0.79,
        final_score_v2=0.76,
        fruiting_v2_breakdown=v2,
    )


def _excluded_result() -> ModelResult:
    return ModelResult(
        species=Species.CANTHARELLUS_CIBARIUS,
        location=Location(59.418, 18.129),
        date=date(2026, 8, 27),
        habitat_score=None,
        fruiting_score=None,
        final_score=None,
        confidence=1.0,
        factors={},
        missing_features=(),
        data_sources={"landcover": "nmd2023"},
        feature_provenance={"landcover_class": _provenance(51)},
        eligibility_status="excluded",
        habitat_exclusions=(
            HabitatExclusion(
                reason_code="landcover_open_water",
                reason_label="Öppet vatten",
                source_feature="landcover_class",
                source_name="nmd2023",
                source_path=r"C:\private\model-data\nmd.tif",
                raw_value=51,
                semantic_status="official_landcover_exclusion",
            ),
        ),
    )


class _StaticService:
    def __init__(self, result: ModelResult) -> None:
        self.result = result
        self.last_date: date | None = None

    def get_score(
        self,
        *,
        latitude: float,
        longitude: float,
        date: date,
        species: Species,
    ) -> ModelResult:
        del latitude, longitude, species
        self.last_date = date
        return self.result


class _MissingWeatherService:
    def get_score(self, **_: object) -> ModelResult:
        raise MesanHistoryError(r"No database at C:\private\weather.sqlite")


@pytest.fixture
def client_factory():
    clients: list[TestClient] = []

    def create(service: object) -> TestClient:
        app = create_app()
        app.dependency_overrides[get_scoring_service] = lambda: service
        client = TestClient(app, raise_server_exceptions=False)
        clients.append(client)
        return client

    yield create

    for client in clients:
        client.close()


def _score_params(**overrides: str) -> dict[str, str]:
    return {
        "latitude": "59.160136",
        "longitude": "18.247348",
        "species": "cantharellus_cibarius",
        "date": "2026-08-27",
        **overrides,
    }


def test_health_is_lightweight(client_factory) -> None:
    response = client_factory(_MissingWeatherService()).get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "fungifind-api"}


def test_valid_forest_point_has_stable_separated_score_blocks(client_factory) -> None:
    response = client_factory(_StaticService(_eligible_result())).get(
        "/api/score", params=_score_params()
    )

    assert response.status_code == 200
    payload = response.json()
    ScoreResponse.model_validate(payload)
    assert payload["production"] == {
        "model_version": "production_v1",
        "habitat_score": 0.81,
        "fruiting_score": 0.67,
        "final_score": 0.75,
        "confidence": 0.93,
    }
    assert payload["experimental"] == {
        "label": "experimental_not_production",
        "model_version": "fruiting_v2_experiment_v1",
        "status": "experimental_complete",
        "fruiting_score_v2": 0.79,
        "final_score_v2": 0.76,
    }
    assert payload["moisture"]["estimated_current_soil_moisture"] == 0.64
    assert payload["factors"]["soil_label"] == "Sandig morän"
    assert payload["debug"] is None


def test_excluded_water_is_200_with_null_production_scores(client_factory) -> None:
    response = client_factory(_StaticService(_excluded_result())).get(
        "/api/score", params=_score_params(latitude="59.418", longitude="18.129")
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["eligibility"]["status"] == "excluded"
    assert payload["eligibility"]["exclusions"][0]["code"] == "landcover_open_water"
    assert payload["production"]["habitat_score"] is None
    assert payload["production"]["fruiting_score"] is None
    assert payload["production"]["final_score"] is None


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"latitude": "91"}, "invalid_coordinates"),
        ({"longitude": "-181"}, "invalid_coordinates"),
        ({"species": "boletus_edulis"}, "invalid_species"),
        ({"date": "27-08-2026"}, "invalid_date"),
    ],
)
def test_invalid_queries_use_machine_readable_errors(
    client_factory, overrides: dict[str, str], code: str
) -> None:
    response = client_factory(_StaticService(_eligible_result())).get(
        "/api/score", params=_score_params(**overrides)
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == code
    assert response.json()["error"]["details"]


def test_missing_weather_returns_safe_machine_readable_error(client_factory) -> None:
    response = client_factory(_MissingWeatherService()).get(
        "/api/score", params=_score_params()
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "weather_history_unavailable"
    assert "C:\\" not in response.text
    assert "private" not in response.text


def test_incomplete_weather_is_not_rendered_as_a_valid_score(client_factory) -> None:
    response = client_factory(
        _StaticService(_eligible_result(moisture_status="insufficient_central_inputs"))
    ).get("/api/score", params=_score_params())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "weather_history_incomplete"


def test_debug_is_opt_in_and_never_exposes_source_paths(client_factory) -> None:
    response = client_factory(_StaticService(_eligible_result())).get(
        "/api/score", params=_score_params(include_debug="true")
    )

    assert response.status_code == 200
    debug = response.json()["debug"]
    assert debug["feature_provenance"]["rainfall_7d_mm"]["available_hours"] == 168
    serialized = response.text.lower()
    assert "source_path" not in serialized
    assert "private" not in serialized
    assert "source.tif" not in serialized


def test_omitted_date_uses_the_current_utc_day(client_factory) -> None:
    service = _StaticService(_eligible_result(target_date=datetime.now(UTC).date()))
    params = _score_params()
    del params["date"]

    response = client_factory(service).get("/api/score", params=params)

    assert response.status_code == 200
    assert service.last_date == datetime.now(UTC).date()
