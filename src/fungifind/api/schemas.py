"""Stable public schemas for the FungiFind point API."""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApiSpecies(str, Enum):
    CANTHARELLUS_CIBARIUS = "cantharellus_cibarius"
    CRATERELLUS_TUBAEFORMIS = "craterellus_tubaeformis"


class HealthResponse(ApiModel):
    status: Literal["ok"]
    service: Literal["fungifind-api"]


class LocationResponse(ApiModel):
    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)


class ExclusionResponse(ApiModel):
    code: str
    label: str
    source_feature: str


class EligibilityResponse(ApiModel):
    status: Literal["eligible", "excluded"]
    exclusions: list[ExclusionResponse]


class ProductionScoresResponse(ApiModel):
    model_version: Literal["production_v1"]
    habitat_score: float | None = Field(default=None, ge=0.0, le=1.0)
    fruiting_score: float | None = Field(default=None, ge=0.0, le=1.0)
    final_score: float | None = Field(default=None, ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)


class ExperimentalScoresResponse(ApiModel):
    label: Literal["experimental_not_production"]
    model_version: Literal["fruiting_v2_experiment_v1"]
    status: str | None
    fruiting_score_v2: float | None = Field(default=None, ge=0.0, le=1.0)
    final_score_v2: float | None = Field(default=None, ge=0.0, le=1.0)


class MoistureResponse(ApiModel):
    estimator_version: Literal["current_soil_moisture_heuristic_v1"]
    status: str | None
    estimated_current_soil_moisture: float | None = Field(
        default=None, ge=0.0, le=1.0
    )
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    completeness: float | None = Field(default=None, ge=0.0, le=1.0)


class TreeProfileResponse(ApiModel):
    spruce_fraction: float | None = Field(default=None, ge=0.0, le=1.0)
    pine_fraction: float | None = Field(default=None, ge=0.0, le=1.0)
    birch_fraction: float | None = Field(default=None, ge=0.0, le=1.0)
    other_deciduous_fraction: float | None = Field(default=None, ge=0.0, le=1.0)


class CompactFactorsResponse(ApiModel):
    landcover_class: int | None = None
    landcover_label: str | None = None
    tree_profile: TreeProfileResponse
    static_wetness_class: int | None = None
    static_wetness_label: str | None = None
    soil_group: str | None = None
    soil_label: str | None = None
    slope_degrees: float | None = Field(default=None, ge=0.0, le=90.0)
    rain_7d_mm: float | None = Field(default=None, ge=0.0)
    rain_30d_mm: float | None = Field(default=None, ge=0.0)
    temp_mean_7d_c: float | None = None
    current_moisture: float | None = Field(default=None, ge=0.0, le=1.0)


class DebugFeatureResponse(ApiModel):
    source_name: str
    semantic_status: str
    quality: float = Field(ge=0.0, le=1.0)
    is_mock: bool
    is_nodata: bool
    raw_value: float | int | None
    interpreted_value: float | int | None
    coverage_status: str | None = None
    coverage_fraction: float | None = Field(default=None, ge=0.0, le=1.0)
    available_hours: int | None = Field(default=None, ge=0)
    expected_hours: int | None = Field(default=None, ge=0)
    unit: str | None = None


class DebugResponse(ApiModel):
    feature_provenance: dict[str, DebugFeatureResponse]
    missing_features: list[str]
    weather_completeness: dict[str, str]
    moisture_missing_inputs: list[str]
    fruiting_v2_missing_inputs: list[str]


class ScoreResponse(ApiModel):
    location: LocationResponse
    species: ApiSpecies
    date: date
    eligibility: EligibilityResponse
    production: ProductionScoresResponse
    experimental: ExperimentalScoresResponse
    moisture: MoistureResponse
    factors: CompactFactorsResponse
    debug: DebugResponse | None = None


class ViewportGeometry(ApiModel):
    type: Literal["Polygon"]
    coordinates: list[list[list[float]]]


class ViewportFeatureProperties(ApiModel):
    cell_id: str
    model_version: Literal["production_v1"]
    eligibility: Literal["eligible"]
    final_index: float = Field(ge=0.0, le=1.0)
    habitat_index: float | None = Field(default=None, ge=0.0, le=1.0)
    fruiting_index: float | None = Field(default=None, ge=0.0, le=1.0)
    data_confidence: float = Field(ge=0.0, le=1.0)


class ViewportFeature(ApiModel):
    type: Literal["Feature"]
    id: str
    geometry: ViewportGeometry
    properties: ViewportFeatureProperties


class ViewportTimingsResponse(ApiModel):
    grid_ms: float = Field(ge=0.0)
    habitat_ms: float = Field(ge=0.0)
    mesan_resolve_ms: float = Field(ge=0.0)
    weather_ms: float = Field(ge=0.0)
    scoring_ms: float = Field(ge=0.0)
    serialization_ms: float = Field(ge=0.0)
    total_ms: float = Field(ge=0.0)


class ViewportMetadata(ApiModel):
    schema_version: Literal["viewport_geojson_v1"]
    model_version: Literal["production_v1"]
    config_version: Literal["viewport_overlay_v1"]
    species: ApiSpecies
    date: date
    requested_bbox: list[float] = Field(min_length=4, max_length=4)
    coverage_bbox: list[float] = Field(min_length=4, max_length=4)
    grid_crs: Literal["EPSG:3006"]
    requested_resolution_m: int = Field(gt=0)
    actual_resolution_m: int = Field(gt=0)
    columns: int = Field(gt=0)
    rows: int = Field(gt=0)
    grid_cell_count: int = Field(gt=0, le=10_000)
    feature_count: int = Field(ge=0, le=10_000)
    eligible_habitat_cell_count: int = Field(ge=0, le=10_000)
    excluded_cell_count: int = Field(ge=0, le=10_000)
    no_data_cell_count: int = Field(ge=0, le=10_000)
    unique_mesan_point_count: int = Field(ge=0, le=10_000)
    eligibility_policy: Literal["excluded_and_no_data_cells_are_omitted"]
    cache_hit: bool
    cache_ttl_seconds: int = Field(gt=0)
    timings: ViewportTimingsResponse


class ViewportResponse(ApiModel):
    type: Literal["FeatureCollection"]
    features: list[ViewportFeature]
    metadata: ViewportMetadata


class ErrorDetail(ApiModel):
    field: str | None = None
    message: str


class ErrorBody(ApiModel):
    code: str
    message: str
    details: list[ErrorDetail] = Field(default_factory=list)


class ErrorResponse(ApiModel):
    error: ErrorBody
