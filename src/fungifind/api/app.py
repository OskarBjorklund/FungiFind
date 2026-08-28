"""FastAPI adapter for one-coordinate FungiFind model evaluation."""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from functools import lru_cache
from pathlib import Path
from time import perf_counter
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from fungifind.api.schemas import (
    ApiSpecies,
    CompactFactorsResponse,
    DebugFeatureResponse,
    DebugResponse,
    EligibilityResponse,
    ErrorBody,
    ErrorDetail,
    ErrorResponse,
    ExclusionResponse,
    ExperimentalScoresResponse,
    HealthResponse,
    LocationResponse,
    MoistureResponse,
    ProductionScoresResponse,
    ScoreResponse,
    TreeProfileResponse,
    ViewportResponse,
)
from fungifind.data_sources import (
    CompositeHabitatDataSource,
    ElevationTileNotFoundError,
    ForestProfileRasterDataSource,
    ForestShareInterpretation,
    MesanHistoryError,
    MesanWeatherHistoryDataSource,
    NmdLandcoverRasterDataSource,
    SguSoilVectorDataSource,
    StaticWetnessRasterDataSource,
    TerrainDemReader,
    VectorPointError,
)
from fungifind.data_sources.raster import RasterPointError
from fungifind.models import (
    DataSourceMetadata,
    FeatureProvenance,
    FeatureSnapshot,
    Location,
    ModelResult,
    Species,
    StaticHabitatFeatures,
)
from fungifind.service import MushroomScoringService
from fungifind.viewport import (
    VIEWPORT_CONFIG_VERSION,
    VIEWPORT_MODEL_VERSION,
    TtlLruCache,
    ViewportBatchEvaluator,
    ViewportBounds,
    ViewportStageTimings,
    ViewportValidationError,
    build_aligned_grid,
    parse_bbox,
    viewport_cache_key,
)

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ApiSettings:
    data_root: Path
    mesan_database: Path
    cors_origins: tuple[str, ...]

    @classmethod
    def from_environment(cls) -> ApiSettings:
        repository_root = Path(__file__).resolve().parents[3]
        data_root = Path(
            os.environ.get("FUNGIFIND_DATA_ROOT", repository_root / "src" / "data")
        ).resolve()
        database = Path(
            os.environ.get(
                "FUNGIFIND_MESAN_DATABASE",
                data_root / "weather" / "mesan_history.sqlite",
            )
        ).resolve()
        configured_origins = os.environ.get(
            "FUNGIFIND_CORS_ORIGINS", "http://localhost:3000"
        )
        origins = tuple(
            origin.strip() for origin in configured_origins.split(",") if origin.strip()
        )
        return cls(data_root=data_root, mesan_database=database, cors_origins=origins)


class _OptionalTerrainDataSource:
    """Return missing optional terrain when no downloaded DEM tile covers a point."""

    def __init__(self, manifest: Path) -> None:
        self.reader = TerrainDemReader.from_manifest(manifest)

    def get_features(self, location: Location) -> FeatureSnapshot[StaticHabitatFeatures]:
        try:
            return self.reader.sample_terrain(location).snapshot
        except ElevationTileNotFoundError:
            return FeatureSnapshot(
                features=StaticHabitatFeatures(),
                metadata=DataSourceMetadata(
                    source_name="lantmateriet_dtm_not_downloaded_at_location",
                    quality=0.0,
                    details={"optional_slope_status": "no_local_dem_tile"},
                ),
            )


def build_scoring_service(settings: ApiSettings | None = None) -> MushroomScoringService:
    resolved = settings or ApiSettings.from_environment()
    data_root = resolved.data_root
    habitat = CompositeHabitatDataSource(
        {
            "tree_species": ForestProfileRasterDataSource.from_kind_directory(
                data_root / "kind",
                interpretation=(
                    ForestShareInterpretation.PROVISIONAL_ZERO_TO_HUNDRED_SHARE
                ),
            ),
            "static_wetness": StaticWetnessRasterDataSource.slu_classified(
                data_root / "misc_data" / "SLUMarkfuktighetKlassad.tif"
            ),
            "landcover": NmdLandcoverRasterDataSource.nmd2023_v2_1(
                data_root / "base_layer" / "NMD2023bas_v2_1.tif"
            ),
            "soil": SguSoilVectorDataSource.official(
                data_root / "soil_type" / "jordarter25k_100k.gpkg"
            ),
            "terrain": _OptionalTerrainDataSource(
                data_root / "elevation" / "manifest.json"
            ),
        }
    )
    weather = MesanWeatherHistoryDataSource(resolved.mesan_database)
    return MushroomScoringService(habitat, weather)


@lru_cache(maxsize=1)
def get_scoring_service() -> MushroomScoringService:
    return build_scoring_service()


def _provenance_value(result: ModelResult, name: str) -> float | int | None:
    item = result.feature_provenance.get(name)
    return None if item is None else item.interpreted_value


def _detail(result: ModelResult, name: str, detail: str) -> Any | None:
    item = result.feature_provenance.get(name)
    return None if item is None else item.details.get(detail)


def _string_detail(result: ModelResult, name: str, detail: str) -> str | None:
    value = _detail(result, name, detail)
    return value if isinstance(value, str) else None


def _debug_feature(item: FeatureProvenance) -> DebugFeatureResponse:
    details = item.details
    coverage_fraction = details.get("coverage_fraction")
    available_hours = details.get("available_hours")
    expected_hours = details.get("expected_hours")
    return DebugFeatureResponse(
        source_name=item.source_name,
        semantic_status=item.semantic_status,
        quality=item.quality,
        is_mock=item.is_mock,
        is_nodata=item.is_nodata,
        raw_value=item.raw_value,
        interpreted_value=item.interpreted_value,
        coverage_status=(
            str(details["coverage_status"])
            if "coverage_status" in details
            else None
        ),
        coverage_fraction=(
            float(coverage_fraction)
            if isinstance(coverage_fraction, (float, int))
            else None
        ),
        available_hours=(
            int(available_hours) if isinstance(available_hours, int) else None
        ),
        expected_hours=(
            int(expected_hours) if isinstance(expected_hours, int) else None
        ),
        unit=str(details["unit"]) if "unit" in details else None,
    )


def build_score_response(result: ModelResult, *, include_debug: bool) -> ScoreResponse:
    moisture = result.moisture_breakdown
    fruiting_v2 = result.fruiting_v2_breakdown
    compact_factors = CompactFactorsResponse(
        landcover_class=_as_int(_provenance_value(result, "landcover_class")),
        landcover_label=_string_detail(
            result, "landcover_class", "official_class_label"
        ),
        tree_profile=TreeProfileResponse(
            spruce_fraction=_as_float(_provenance_value(result, "spruce_fraction")),
            pine_fraction=_as_float(_provenance_value(result, "pine_fraction")),
            birch_fraction=_as_float(_provenance_value(result, "birch_fraction")),
            other_deciduous_fraction=_as_float(
                _provenance_value(result, "other_deciduous_fraction")
            ),
        ),
        static_wetness_class=_as_int(
            _provenance_value(result, "static_wetness_class")
        ),
        static_wetness_label=_string_detail(
            result, "static_wetness_class", "interpreted_class_label"
        ),
        soil_group=_string_detail(result, "soil_group", "interpreted_soil_group"),
        soil_label=_string_detail(result, "soil_group", "raw_official_label"),
        slope_degrees=_as_float(_provenance_value(result, "slope_degrees")),
        rain_7d_mm=_as_float(_provenance_value(result, "rainfall_7d_mm")),
        rain_30d_mm=_as_float(_provenance_value(result, "rainfall_30d_mm")),
        temp_mean_7d_c=_as_float(_provenance_value(result, "temp_mean_7d_c")),
        current_moisture=result.estimated_current_soil_moisture,
    )
    debug = None
    if include_debug:
        debug = DebugResponse(
            feature_provenance={
                name: _debug_feature(item)
                for name, item in sorted(result.feature_provenance.items())
            },
            missing_features=list(result.missing_features),
            weather_completeness=dict(result.weather_completeness),
            moisture_missing_inputs=(
                [] if moisture is None else list(moisture.missing_inputs)
            ),
            fruiting_v2_missing_inputs=(
                [] if fruiting_v2 is None else list(fruiting_v2.missing_inputs)
            ),
        )

    return ScoreResponse(
        location=LocationResponse(
            latitude=result.location.latitude,
            longitude=result.location.longitude,
        ),
        species=ApiSpecies(result.species.value),
        date=result.date,
        eligibility=EligibilityResponse(
            status=result.eligibility_status,
            exclusions=[
                ExclusionResponse(
                    code=item.reason_code,
                    label=item.reason_label,
                    source_feature=item.source_feature,
                )
                for item in result.habitat_exclusions
            ],
        ),
        production=ProductionScoresResponse(
            model_version="production_v1",
            habitat_score=result.habitat_score,
            fruiting_score=result.fruiting_score,
            final_score=result.final_score,
            confidence=result.confidence,
        ),
        experimental=ExperimentalScoresResponse(
            label="experimental_not_production",
            model_version="fruiting_v2_experiment_v1",
            status=None if fruiting_v2 is None else fruiting_v2.status,
            fruiting_score_v2=result.fruiting_score_v2,
            final_score_v2=result.final_score_v2,
        ),
        moisture=MoistureResponse(
            estimator_version="current_soil_moisture_heuristic_v1",
            status=None if moisture is None else moisture.status,
            estimated_current_soil_moisture=(
                result.estimated_current_soil_moisture
            ),
            confidence=None if moisture is None else moisture.confidence,
            completeness=None if moisture is None else moisture.completeness,
        ),
        factors=compact_factors,
        debug=debug,
    )


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    return float(value)


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _error_response(
    status_code: int,
    code: str,
    message: str,
    details: list[ErrorDetail] | None = None,
) -> JSONResponse:
    payload = ErrorResponse(
        error=ErrorBody(code=code, message=message, details=details or [])
    )
    return JSONResponse(status_code=status_code, content=payload.model_dump())


def _validation_code(exc: RequestValidationError) -> str:
    fields = {str(item) for error in exc.errors() for item in error.get("loc", ())}
    if "species" in fields:
        return "invalid_species"
    if fields.intersection({"latitude", "longitude"}):
        return "invalid_coordinates"
    if "date" in fields:
        return "invalid_date"
    if fields.intersection({"bbox", "west", "south", "east", "north"}):
        return "invalid_bbox"
    if fields.intersection({"resolution", "resolution_m"}):
        return "invalid_resolution"
    return "validation_error"


def create_app() -> FastAPI:
    settings = ApiSettings.from_environment()
    viewport_cache: TtlLruCache[ViewportResponse] = TtlLruCache(max_entries=32)
    application = FastAPI(
        title="FungiFind API",
        version="0.1.0",
        description=(
            "Point lookup plus a bounded production-v1 viewport suitability-index grid."
        ),
    )
    application.state.viewport_cache = viewport_cache
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["Accept", "Content-Type"],
    )

    @application.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        del request
        details = [
            ErrorDetail(
                field=".".join(str(part) for part in error.get("loc", ())[1:]) or None,
                message=str(error.get("msg", "Invalid value")),
            )
            for error in exc.errors()
        ]
        return _error_response(
            422,
            _validation_code(exc),
            "The request parameters are invalid.",
            details,
        )

    @application.exception_handler(ViewportValidationError)
    async def viewport_validation_exception_handler(
        request: Request, exc: ViewportValidationError
    ) -> JSONResponse:
        del request
        return _error_response(
            422,
            exc.code,
            str(exc),
            [ErrorDetail(field=exc.field, message=str(exc))],
        )

    @application.exception_handler(MesanHistoryError)
    async def weather_exception_handler(
        request: Request, exc: MesanHistoryError
    ) -> JSONResponse:
        del request
        LOGGER.info("MESAN history unavailable: %s", exc)
        return _error_response(
            503,
            "weather_history_unavailable",
            "Complete MESAN weather history is unavailable for this point and date.",
        )

    @application.exception_handler(FileNotFoundError)
    async def missing_source_handler(
        request: Request, exc: FileNotFoundError
    ) -> JSONResponse:
        del request
        LOGGER.error("Required local source is missing: %s", exc)
        return _error_response(
            503,
            "source_data_unavailable",
            "A required local model data source is unavailable.",
        )

    @application.exception_handler(RasterPointError)
    @application.exception_handler(VectorPointError)
    async def point_source_handler(
        request: Request, exc: RasterPointError | VectorPointError
    ) -> JSONResponse:
        del request
        LOGGER.info("Point cannot be sampled: %s", exc)
        return _error_response(
            422,
            "point_outside_data_coverage",
            "The coordinate is outside available habitat data coverage.",
        )

    @application.exception_handler(OSError)
    @application.exception_handler(RuntimeError)
    async def source_failure_handler(
        request: Request, exc: OSError | RuntimeError
    ) -> JSONResponse:
        del request
        LOGGER.error(
            "Model data source failed",
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        return _error_response(
            503,
            "data_source_failure",
            "A model data source failed while evaluating the point.",
        )

    @application.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        del request
        LOGGER.error(
            "Unexpected API error",
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        return _error_response(
            500,
            "internal_error",
            "The point could not be evaluated because of an internal error.",
        )

    @application.get("/api/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(status="ok", service="fungifind-api")

    @application.get(
        "/api/score",
        response_model=ScoreResponse,
        responses={
            422: {"model": ErrorResponse},
            500: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
    )
    def score(
        latitude: Annotated[float, Query(ge=-90.0, le=90.0)],
        longitude: Annotated[float, Query(ge=-180.0, le=180.0)],
        species: ApiSpecies,
        service: Annotated[MushroomScoringService, Depends(get_scoring_service)],
        target_date: Annotated[date | None, Query(alias="date")] = None,
        include_debug: bool = False,
    ) -> ScoreResponse | JSONResponse:
        selected_date = target_date or datetime.now(UTC).date()
        result = service.get_score(
            latitude=latitude,
            longitude=longitude,
            date=selected_date,
            species=Species(species.value),
        )
        if (
            result.eligibility_status == "eligible"
            and result.moisture_breakdown is not None
            and result.moisture_breakdown.status == "insufficient_central_inputs"
        ):
            return _error_response(
                503,
                "weather_history_incomplete",
                "The selected date does not have complete 30-day MESAN history.",
            )
        return build_score_response(result, include_debug=include_debug)

    @application.get(
        "/api/viewport",
        response_model=ViewportResponse,
        response_model_exclude_none=True,
        responses={
            422: {"model": ErrorResponse},
            500: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
    )
    def viewport(
        response: Response,
        species: ApiSpecies,
        service: Annotated[MushroomScoringService, Depends(get_scoring_service)],
        bbox: Annotated[str | None, Query(min_length=7, max_length=160)] = None,
        west: float | None = None,
        south: float | None = None,
        east: float | None = None,
        north: float | None = None,
        resolution_m: Annotated[int | None, Query(gt=0)] = None,
        resolution: Annotated[int | None, Query(gt=0)] = None,
        target_date: Annotated[date | None, Query(alias="date")] = None,
    ) -> ViewportResponse:
        selected_date = target_date or datetime.now(UTC).date()
        split_bounds = (west, south, east, north)
        has_any_split_bound = any(value is not None for value in split_bounds)
        has_all_split_bounds = all(value is not None for value in split_bounds)
        if bbox is not None and has_any_split_bound:
            raise ViewportValidationError(
                "invalid_bbox",
                "Use either bbox or west/south/east/north, not both",
                field="bbox",
            )
        if bbox is not None:
            requested_bounds = parse_bbox(bbox)
        elif has_all_split_bounds:
            requested_bounds = ViewportBounds(
                west=float(west),
                south=float(south),
                east=float(east),
                north=float(north),
            )
        else:
            raise ViewportValidationError(
                "invalid_bbox",
                "Provide bbox or all of west, south, east and north",
                field="bbox",
            )
        if (
            resolution_m is not None
            and resolution is not None
            and resolution_m != resolution
        ):
            raise ViewportValidationError(
                "invalid_resolution",
                "resolution_m and resolution cannot disagree",
                field="resolution_m",
            )
        selected_resolution = resolution_m or resolution
        if selected_resolution is None:
            raise ViewportValidationError(
                "invalid_resolution",
                "resolution_m is required",
                field="resolution_m",
            )
        grid_started = perf_counter()
        grid = build_aligned_grid(requested_bounds, selected_resolution)
        grid_ms = (perf_counter() - grid_started) * 1000
        key = viewport_cache_key(
            grid,
            Species(species.value),
            selected_date,
        )
        ttl_seconds = (
            60 if selected_date >= datetime.now(UTC).date() else 3_600
        )
        cached = viewport_cache.get(key)
        if cached is not None:
            response.headers["X-FungiFind-Viewport-Cache"] = "HIT"
            response.headers["Cache-Control"] = f"private, max-age={ttl_seconds}"
            return cached.model_copy(
                update={
                    "metadata": cached.metadata.model_copy(
                        update={"cache_hit": True}
                    )
                }
            )

        evaluation = ViewportBatchEvaluator(service).evaluate(
            grid,
            Species(species.value),
            selected_date,
        )
        stage = evaluation.timings
        timings = ViewportStageTimings(
            grid_ms=round(grid_ms, 3),
            habitat_ms=stage.habitat_ms,
            mesan_resolve_ms=stage.mesan_resolve_ms,
            weather_ms=stage.weather_ms,
            scoring_ms=stage.scoring_ms,
            serialization_ms=stage.serialization_ms,
            total_ms=round(grid_ms + stage.total_ms, 3),
        )
        result = ViewportResponse.model_validate(
            {
                "type": "FeatureCollection",
                "features": list(evaluation.features),
                "metadata": {
                    "schema_version": "viewport_geojson_v1",
                    "model_version": VIEWPORT_MODEL_VERSION,
                    "config_version": VIEWPORT_CONFIG_VERSION,
                    "species": species,
                    "date": selected_date,
                    "requested_bbox": requested_bounds.as_list(),
                    "coverage_bbox": grid.coverage_bounds.as_list(),
                    "grid_crs": "EPSG:3006",
                    "requested_resolution_m": grid.requested_resolution_m,
                    "actual_resolution_m": grid.resolution_m,
                    "columns": grid.columns,
                    "rows": grid.rows,
                    "grid_cell_count": grid.cell_count,
                    "feature_count": len(evaluation.features),
                    "eligible_habitat_cell_count": (
                        evaluation.eligible_habitat_cell_count
                    ),
                    "excluded_cell_count": evaluation.excluded_cell_count,
                    "no_data_cell_count": evaluation.no_data_cell_count,
                    "unique_mesan_point_count": (
                        evaluation.unique_mesan_point_count
                    ),
                    "eligibility_policy": (
                        "excluded_and_no_data_cells_are_omitted"
                    ),
                    "cache_hit": False,
                    "cache_ttl_seconds": ttl_seconds,
                    "timings": asdict(timings),
                },
            }
        )
        # Publish only a complete response, so cancellation or evaluation
        # failures cannot leave partial cache state behind.
        viewport_cache.set(key, result, ttl_seconds)
        response.headers["X-FungiFind-Viewport-Cache"] = "MISS"
        response.headers["Cache-Control"] = f"private, max-age={ttl_seconds}"
        return result

    return application


app = create_app()
