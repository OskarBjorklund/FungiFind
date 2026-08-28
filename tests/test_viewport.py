from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time

import pytest
from fastapi.testclient import TestClient
from pyproj import Transformer

from fungifind.api.app import create_app, get_scoring_service
from fungifind.api.schemas import ViewportResponse
from fungifind.data_sources.mesan_weather import (
    MesanWeatherHistoryDataSource,
    MesanWeatherResolution,
)
from fungifind.data_sources.mock import MockHabitatDataSource, MockWeatherDataSource
from fungifind.data_sources.raster_features import CompositeHabitatDataSource
from fungifind.data_sources.smhi_mesan import MesanGridPoint
from fungifind.models import (
    DataSourceMetadata,
    FeatureProvenance,
    FeatureSnapshot,
    Location,
    Species,
    StaticHabitatFeatures,
)
from fungifind.service import MushroomScoringService
from fungifind.viewport import (
    MAX_GRID_CELLS,
    TtlLruCache,
    ViewportBatchEvaluator,
    ViewportBounds,
    ViewportValidationError,
    build_aligned_grid,
    viewport_cache_key,
)

_TO_WGS84 = Transformer.from_crs("EPSG:3006", "EPSG:4326", always_xy=True)


def _projected_bbox(x0: float, y0: float, x1: float, y1: float) -> ViewportBounds:
    longitudes, latitudes = _TO_WGS84.transform(
        [x0, x1, x1, x0], [y0, y0, y1, y1]
    )
    return ViewportBounds(
        west=min(longitudes),
        south=min(latitudes),
        east=max(longitudes),
        north=max(latitudes),
    )


def _service(
    habitat: object | None = None, weather: object | None = None
) -> MushroomScoringService:
    return MushroomScoringService(
        habitat_source=habitat or MockHabitatDataSource(),  # type: ignore[arg-type]
        weather_source=weather or MockWeatherDataSource(),  # type: ignore[arg-type]
    )


def _params(**overrides: str) -> dict[str, str]:
    return {
        "bbox": "18.2300,59.1500,18.2320,59.1520",
        "species": "cantharellus_cibarius",
        "date": "2026-08-27",
        "resolution": "200",
        **overrides,
    }


@pytest.fixture
def viewport_client():
    app = create_app()
    app.dependency_overrides[get_scoring_service] = _service
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def test_grid_is_globally_aligned_and_cell_ids_are_stable() -> None:
    bounds = ViewportBounds(18.2, 59.1, 18.21, 59.11)

    first = build_aligned_grid(bounds, 50)
    second = build_aligned_grid(bounds, 50)

    assert first.projected_bounds == second.projected_bounds
    assert [cell.cell_id for cell in first.cells] == [
        cell.cell_id for cell in second.cells
    ]
    assert all(value % 50 == 0 for value in first.projected_bounds)
    assert all(cell.polygon[0] == cell.polygon[-1] for cell in first.cells)


def test_overlap_from_separate_viewports_uses_identical_cell_identity() -> None:
    broad = build_aligned_grid(ViewportBounds(18.20, 59.10, 18.22, 59.12), 100)
    nested = build_aligned_grid(ViewportBounds(18.205, 59.105, 18.215, 59.115), 100)

    broad_cells = {cell.cell_id: cell.polygon for cell in broad.cells}
    overlap = [cell for cell in nested.cells if cell.cell_id in broad_cells]

    assert overlap
    assert all(broad_cells[cell.cell_id] == cell.polygon for cell in overlap)


def test_grid_auto_coarsens_deterministically_below_cell_cap() -> None:
    bounds = _projected_bbox(670_000, 6_570_000, 680_000, 6_580_000)

    grid = build_aligned_grid(bounds, 100)

    assert grid.resolution_m == 200
    assert grid.cell_count <= MAX_GRID_CELLS
    assert grid.requested_resolution_m == 100


@pytest.mark.parametrize("resolution", [0, 24, 75, 400])
def test_grid_rejects_unadvertised_requested_resolution(resolution: int) -> None:
    with pytest.raises(ViewportValidationError, match="resolution must be one of"):
        build_aligned_grid(ViewportBounds(18.2, 59.1, 18.21, 59.11), resolution)


def test_grid_rejects_oversized_bbox_before_allocating_cells() -> None:
    bounds = _projected_bbox(600_000, 6_500_000, 701_000, 6_510_000)

    with pytest.raises(ViewportValidationError) as error:
        build_aligned_grid(bounds, 200)

    assert error.value.code == "bbox_too_large"


class _AlternatingExclusionHabitat:
    def __init__(self) -> None:
        self.base = MockHabitatDataSource()
        self.calls = 0

    def get_features(self, location):
        self.calls += 1
        snapshot = self.base.get_features(location)
        if self.calls % 2 == 0:
            return snapshot
        exclusion = FeatureProvenance(
            source_name="test_landcover",
            quality=1.0,
            is_mock=False,
            semantic_status="validated_test_exclusion",
            raw_value=51,
            interpreted_value=51,
            details={
                "habitat_exclusion_code": "test_water",
                "habitat_exclusion_label": "Testvatten",
            },
        )
        return FeatureSnapshot(
            features=snapshot.features,
            metadata=snapshot.metadata,
            feature_provenance={"landcover_class": exclusion},
        )


class _CountingWeather:
    def __init__(self) -> None:
        self.base = MockWeatherDataSource()
        self.calls = 0

    def get_features(self, location, target_date):
        self.calls += 1
        return self.base.get_features(location, target_date)


def test_batch_evaluator_skips_weather_for_excluded_habitat() -> None:
    grid = build_aligned_grid(ViewportBounds(18.23, 59.15, 18.232, 59.152), 200)
    grid = replace(grid, cells=grid.cells[:4], columns=2, rows=2)
    habitat = _AlternatingExclusionHabitat()
    weather = _CountingWeather()

    result = ViewportBatchEvaluator(_service(habitat, weather)).evaluate(
        grid, Species.CANTHARELLUS_CIBARIUS, date(2026, 8, 27)
    )

    assert result.excluded_cell_count == 2
    assert result.eligible_habitat_cell_count == 2
    assert weather.calls == 2
    assert len(result.features) == 2
    assert all(feature["properties"]["eligibility"] == "eligible" for feature in result.features)


class _AlternatingLandcoverBatch:
    def get_features_many(self, locations):
        snapshots = []
        for index, _ in enumerate(locations):
            excluded = index % 2 == 0
            details = (
                {
                    "habitat_exclusion_code": "test_water",
                    "habitat_exclusion_label": "Testvatten",
                }
                if excluded
                else {}
            )
            provenance = FeatureProvenance(
                source_name="test_landcover",
                quality=1.0,
                is_mock=False,
                semantic_status="validated_test_landcover",
                raw_value=51 if excluded else 111,
                interpreted_value=51 if excluded else 111,
                details=details,
            )
            snapshots.append(
                FeatureSnapshot(
                    features=StaticHabitatFeatures(
                        landcover_class=51 if excluded else 111
                    ),
                    metadata=DataSourceMetadata(
                        source_name="test_landcover",
                        quality=1.0,
                    ),
                    feature_provenance={"landcover_class": provenance},
                )
            )
        return tuple(snapshots)


class _CountingExpensiveHabitat:
    def __init__(self) -> None:
        self.base = MockHabitatDataSource()
        self.batch_sizes: list[int] = []

    def get_features_many(self, locations):
        self.batch_sizes.append(len(locations))
        return tuple(self.base.get_features(location) for location in locations)


def test_composite_preflight_skips_expensive_habitat_for_excluded_cells() -> None:
    grid = build_aligned_grid(
        ViewportBounds(18.23, 59.15, 18.232, 59.152), 200
    )
    grid = replace(grid, cells=grid.cells[:4], columns=2, rows=2)
    expensive = _CountingExpensiveHabitat()
    habitat = CompositeHabitatDataSource(
        {
            "tree_species": expensive,
            "landcover": _AlternatingLandcoverBatch(),
        }
    )
    weather = _CountingWeather()

    result = ViewportBatchEvaluator(_service(habitat, weather)).evaluate(
        grid, Species.CANTHARELLUS_CIBARIUS, date(2026, 8, 27)
    )

    assert expensive.batch_sizes == [2]
    assert weather.calls == 2
    assert result.excluded_cell_count == 2
    assert len(result.features) == 2


def test_geojson_contains_production_indices_only() -> None:
    grid = build_aligned_grid(ViewportBounds(18.23, 59.15, 18.232, 59.152), 200)
    grid = replace(grid, cells=grid.cells[:1], columns=1, rows=1)

    result = ViewportBatchEvaluator(_service()).evaluate(
        grid, Species.CANTHARELLUS_CIBARIUS, date(2026, 8, 27)
    )

    properties = result.features[0]["properties"]
    assert set(properties) == {
        "cell_id",
        "model_version",
        "eligibility",
        "final_index",
        "habitat_index",
        "fruiting_index",
        "data_confidence",
    }
    assert not any("probability" in name for name in properties)
    assert not any("v2" in name or "moisture" in name for name in properties)


def test_viewport_cell_matches_point_production_score_at_same_center() -> None:
    service = _service()
    grid = build_aligned_grid(
        ViewportBounds(18.23, 59.15, 18.232, 59.152), 200
    )
    grid = replace(grid, cells=grid.cells[:1], columns=1, rows=1)
    cell = grid.cells[0]

    viewport = ViewportBatchEvaluator(service).evaluate(
        grid, Species.CANTHARELLUS_CIBARIUS, date(2026, 8, 27)
    )
    point = service.get_score(
        cell.center.latitude,
        cell.center.longitude,
        date(2026, 8, 27),
        Species.CANTHARELLUS_CIBARIUS,
    )

    assert viewport.features[0]["properties"]["final_index"] == point.final_score


class _MissingSlopeHabitat:
    def __init__(self) -> None:
        self.base = MockHabitatDataSource()

    def get_features(self, location):
        snapshot = self.base.get_features(location)
        return FeatureSnapshot(
            features=replace(snapshot.features, slope_degrees=None),
            metadata=snapshot.metadata,
            feature_provenance=snapshot.feature_provenance,
        )


def test_missing_dem_slope_does_not_make_viewport_cell_unavailable() -> None:
    grid = build_aligned_grid(
        ViewportBounds(18.23, 59.15, 18.232, 59.152), 200
    )
    grid = replace(grid, cells=grid.cells[:1], columns=1, rows=1)

    result = ViewportBatchEvaluator(_service(habitat=_MissingSlopeHabitat())).evaluate(
        grid, Species.CANTHARELLUS_CIBARIUS, date(2026, 8, 27)
    )

    assert result.no_data_cell_count == 0
    assert len(result.features) == 1


def test_viewport_output_is_deterministic_for_same_inputs() -> None:
    service = _service()
    grid = build_aligned_grid(
        ViewportBounds(18.23, 59.15, 18.232, 59.152), 200
    )

    first = ViewportBatchEvaluator(service).evaluate(
        grid, Species.CANTHARELLUS_CIBARIUS, date(2026, 8, 27)
    )
    second = ViewportBatchEvaluator(service).evaluate(
        grid, Species.CANTHARELLUS_CIBARIUS, date(2026, 8, 27)
    )

    assert first.features == second.features


class _SingleGridMesan(MesanWeatherHistoryDataSource):
    def __init__(self) -> None:
        self.point = MesanGridPoint(59.16, 18.25)
        self.snap_calls = 0
        self.feature_calls = 0
        self.base = MockWeatherDataSource()

    def snap_grid_point(self, location):
        del location
        self.snap_calls += 1
        return self.point, 100.0

    def resolve_snapped(
        self, location, target_date, grid_point, grid_distance_m
    ) -> MesanWeatherResolution:
        return MesanWeatherResolution(
            requested_point=MesanGridPoint(location.latitude, location.longitude),
            grid_point=grid_point,
            grid_distance_m=grid_distance_m,
            target_time=datetime.combine(target_date, time(23), tzinfo=UTC),
        )

    def get_features_for_resolution(self, resolution):
        self.feature_calls += 1
        return self.base.get_features(
            location=Location(
                resolution.requested_point.latitude,
                resolution.requested_point.longitude,
            ),
            target_date=resolution.target_time.date(),
        )


def test_batch_groups_weather_by_snapped_mesan_point_and_date() -> None:
    grid = build_aligned_grid(
        ViewportBounds(18.23, 59.15, 18.232, 59.152), 200
    )
    grid = replace(grid, cells=grid.cells[:4], columns=2, rows=2)
    mesan = _SingleGridMesan()

    result = ViewportBatchEvaluator(_service(weather=mesan)).evaluate(
        grid, Species.CANTHARELLUS_CIBARIUS, date(2026, 8, 27)
    )

    assert mesan.snap_calls == 4
    assert mesan.feature_calls == 1
    assert result.unique_mesan_point_count == 1
    assert len(result.features) == 4


def test_viewport_endpoint_returns_valid_compact_geojson_and_metadata(
    viewport_client: TestClient,
) -> None:
    response = viewport_client.get("/api/viewport", params=_params())

    assert response.status_code == 200
    payload = response.json()
    ViewportResponse.model_validate(payload)
    assert response.headers["X-FungiFind-Viewport-Cache"] == "MISS"
    assert payload["type"] == "FeatureCollection"
    assert payload["metadata"]["grid_crs"] == "EPSG:3006"
    assert payload["metadata"]["grid_cell_count"] <= MAX_GRID_CELLS
    assert payload["metadata"]["feature_count"] == len(payload["features"])
    assert payload["metadata"]["model_version"] == "production_v1"


def test_viewport_endpoint_accepts_explicit_bounds_and_resolution_m(
    viewport_client: TestClient,
) -> None:
    response = viewport_client.get(
        "/api/viewport",
        params={
            "west": "18.2300",
            "south": "59.1500",
            "east": "18.2320",
            "north": "59.1520",
            "species": "cantharellus_cibarius",
            "date": "2026-08-27",
            "resolution_m": "200",
        },
    )

    assert response.status_code == 200
    assert response.json()["metadata"]["actual_resolution_m"] == 200


def test_viewport_endpoint_reuses_aligned_backend_cache(
    viewport_client: TestClient,
) -> None:
    first = viewport_client.get("/api/viewport", params=_params())
    second = viewport_client.get("/api/viewport", params=_params())

    assert first.status_code == second.status_code == 200
    assert first.headers["X-FungiFind-Viewport-Cache"] == "MISS"
    assert second.headers["X-FungiFind-Viewport-Cache"] == "HIT"
    assert second.json()["metadata"]["cache_hit"] is True
    assert first.json()["features"] == second.json()["features"]


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"bbox": "18.2,59.1,18.3"}, "invalid_bbox"),
        ({"bbox": "18.3,59.1,18.2,59.2"}, "invalid_bbox"),
        ({"bbox": "west,59.1,18.3,59.2"}, "invalid_bbox"),
        ({"resolution": "75"}, "invalid_resolution"),
        ({"species": "boletus_edulis"}, "invalid_species"),
        ({"date": "27-08-2026"}, "invalid_date"),
    ],
)
def test_viewport_validation_returns_clear_machine_readable_4xx(
    viewport_client: TestClient,
    overrides: dict[str, str],
    code: str,
) -> None:
    response = viewport_client.get("/api/viewport", params=_params(**overrides))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == code
    assert response.json()["error"]["details"]


def test_cache_key_includes_species_date_resolution_bounds_and_versions() -> None:
    grid = build_aligned_grid(ViewportBounds(18.2, 59.1, 18.21, 59.11), 100)
    other_resolution = build_aligned_grid(
        ViewportBounds(18.2, 59.1, 18.21, 59.11), 200
    )
    day = date(2026, 8, 27)

    base = viewport_cache_key(grid, Species.CANTHARELLUS_CIBARIUS, day)

    assert base != viewport_cache_key(grid, Species.CRATERELLUS_TUBAEFORMIS, day)
    assert base != viewport_cache_key(grid, Species.CANTHARELLUS_CIBARIUS, date(2026, 8, 26))
    assert base != viewport_cache_key(
        other_resolution, Species.CANTHARELLUS_CIBARIUS, day
    )
    assert base[-2:] == ("production_v1", "viewport_overlay_v1")


def test_ttl_lru_is_bounded_and_can_be_cleared() -> None:
    cache: TtlLruCache[str] = TtlLruCache(max_entries=2)
    cache.set("a", "first", 30)
    cache.set("b", "second", 30)
    assert cache.get("a") == "first"
    cache.set("c", "third", 30)

    assert cache.get("b") is None
    assert cache.get("a") == "first"
    assert cache.get("c") == "third"
    cache.clear()
    assert cache.get("a") is None


def test_current_day_uses_shorter_cache_ttl() -> None:
    app = create_app()
    app.dependency_overrides[get_scoring_service] = _service
    params = _params(date=datetime.now(UTC).date().isoformat())

    with TestClient(app) as client:
        response = client.get("/api/viewport", params=params)

    assert response.status_code == 200
    assert response.json()["metadata"]["cache_ttl_seconds"] == 60


class _RecoveringHabitat:
    def __init__(self) -> None:
        self.fail = True
        self.base = MockHabitatDataSource()

    def get_features(self, location):
        if self.fail:
            raise RuntimeError("interrupted batch")
        return self.base.get_features(location)


def test_failed_batch_does_not_publish_partial_cache_state() -> None:
    habitat = _RecoveringHabitat()
    service = _service(habitat=habitat)
    app = create_app()
    app.dependency_overrides[get_scoring_service] = lambda: service

    with TestClient(app, raise_server_exceptions=False) as client:
        failed = client.get("/api/viewport", params=_params())
        habitat.fail = False
        recovered = client.get("/api/viewport", params=_params())

    assert failed.status_code == 503
    assert recovered.status_code == 200
    assert recovered.headers["X-FungiFind-Viewport-Cache"] == "MISS"
