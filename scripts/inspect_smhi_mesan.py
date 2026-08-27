"""Inspect SMHI MESAN2G v3 metadata and one snapped point response."""

from __future__ import annotations

import argparse
import gzip
import json
import math
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE_URL = "https://opendata-download-metanalys.smhi.se/api/category/mesan2g/version/3"
USER_AGENT = "FungiFind-MESAN-inspector/0.1"
DEFAULT_LATITUDE = 59.412
DEFAULT_LONGITUDE = 18.132

JsonObject = dict[str, Any]
UrlOpener = Callable[..., Any]


class MesanInspectionError(RuntimeError):
    """Raised when MESAN metadata is unavailable or has an unexpected shape."""


@dataclass(frozen=True, slots=True)
class ParameterMetadata:
    name: str
    level_type: str
    level: float | int
    unit: str
    description: str


@dataclass(frozen=True, slots=True)
class TimeCoverage:
    first: datetime
    last: datetime
    count: int
    spacing_seconds: tuple[int, ...]

    @property
    def is_uniform(self) -> bool:
        return len(self.spacing_seconds) <= 1

    @property
    def timestep_seconds(self) -> int | None:
        return self.spacing_seconds[0] if len(self.spacing_seconds) == 1 else None

    @property
    def timestamp_span_hours(self) -> float:
        return (self.last - self.first).total_seconds() / 3600


@dataclass(frozen=True, slots=True)
class PointAnalysis:
    requested_longitude: float
    requested_latitude: float
    returned_longitude: float
    returned_latitude: float
    snapped: bool
    snap_distance_m: float


@dataclass(frozen=True, slots=True)
class GridAnalysis:
    geometry_type: str
    coordinate_count: int
    returned_coordinate_present: bool
    approximate_spacing_m: float | None


@dataclass(frozen=True, slots=True)
class HistoryAssessment:
    feature: str
    sufficient: bool
    reason: str


@dataclass(frozen=True, slots=True)
class MesanInspection:
    created_time: str
    reference_time: str
    parameters: tuple[ParameterMetadata, ...]
    relevance: Mapping[str, tuple[str, ...]]
    time_coverage: TimeCoverage
    point: PointAnalysis
    point_time_series_count: int
    grid: GridAnalysis
    history: tuple[HistoryAssessment, ...]
    request_urls: tuple[str, ...]


# These categories repeat only what the parameter name/description says. They do
# not assign biological meaning or introduce undocumented derived variables.
RELEVANT_PARAMETER_NAMES: Mapping[str, tuple[str, ...]] = {
    "air temperature": ("air_temperature",),
    "precipitation": (
        "precipitation_amount_last_1_hours",
        "precipitation_amount_last_3_hours",
        "precipitation_frozen_part",
        "predominant_precipitation_type_at_surface",
    ),
    "relative humidity": ("relative_humidity",),
    "wind": (
        "wind_from_direction",
        "wind_speed",
        "wind_speed_of_gust",
    ),
    "cloudiness": (
        "cloud_base_altitude",
        "cloud_area_fraction",
        "cloud_top_altitude",
        "low_type_cloud_area_fraction",
        "high_type_cloud_area_fraction",
        "medium_type_cloud_area_fraction",
    ),
    "radiation": (),
    "soil or surface": (
        "change_over_time_in_surface_snow_amount_1_hours",
        "predominant_precipitation_type_at_surface",
    ),
}

RAIN_WINDOWS_DAYS: Mapping[str, int] = {
    "rain_1d": 1,
    "rain_3d": 3,
    "rain_7d": 7,
    "rain_14d": 14,
    "rain_21d": 21,
    "rain_30d": 30,
}
TEMPERATURE_WINDOWS_DAYS: Mapping[str, int] = {
    "temp_mean_3d": 3,
    "temp_mean_7d": 7,
    "temp_mean_14d": 14,
}


class SmhiMesanClient:
    """Five-call client for MESAN metadata, one point, and multipoint geometry."""

    def __init__(
        self,
        base_url: str = BASE_URL,
        *,
        timeout: float = 60.0,
        opener: UrlOpener = urlopen,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.opener = opener
        self.request_urls: list[str] = []

    def get_json(self, path: str) -> JsonObject:
        url = f"{self.base_url}/{path.lstrip('/')}"
        self.request_urls.append(url)
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                status = int(getattr(response, "status", 200))
                if not 200 <= status < 300:
                    raise MesanInspectionError(f"HTTP {status} for {url}")
                raw = response.read()
                content_encoding = response.headers.get("Content-Encoding", "")
        except HTTPError as exc:
            raise MesanInspectionError(
                f"HTTP {exc.code} for {url}: {exc.reason}"
            ) from exc
        except URLError as exc:
            raise MesanInspectionError(f"Could not reach {url}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise MesanInspectionError(f"Timed out requesting {url}") from exc

        if content_encoding.casefold() == "gzip":
            try:
                raw = gzip.decompress(raw)
            except gzip.BadGzipFile as exc:
                raise MesanInspectionError(f"Invalid gzip response from {url}") from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MesanInspectionError(f"Invalid JSON returned by {url}") from exc
        if not isinstance(payload, dict):
            raise MesanInspectionError(f"Expected a JSON object from {url}")
        return payload

    def fetch_all(
        self, latitude: float, longitude: float
    ) -> tuple[JsonObject, JsonObject, JsonObject, JsonObject, JsonObject]:
        point_path = (
            f"geotype/point/lon/{format(longitude, '.12g')}"
            f"/lat/{format(latitude, '.12g')}/data.json"
        )
        return (
            self.get_json("createdtime.json"),
            self.get_json("parameter.json"),
            self.get_json("times.json"),
            self.get_json(point_path),
            # This is only the MultiPoint geometry/coordinates, not gridded
            # parameter values for all points and times.
            self.get_json("geotype/multipoint.json"),
        )


def _required_string(payload: Mapping[str, Any], key: str, context: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise MesanInspectionError(f"{context} has no valid {key!r}")
    return value


def parse_parameters(payload: Mapping[str, Any]) -> tuple[ParameterMetadata, ...]:
    raw_parameters = payload.get("parameter")
    if not isinstance(raw_parameters, list):
        raise MesanInspectionError("parameter.json has no parameter list")
    parameters: list[ParameterMetadata] = []
    for index, item in enumerate(raw_parameters):
        if not isinstance(item, dict):
            raise MesanInspectionError(f"Parameter {index} is not an object")
        level = item.get("level")
        if isinstance(level, bool) or not isinstance(level, (int, float)):
            raise MesanInspectionError(f"Parameter {index} has no numeric level")
        parameters.append(
            ParameterMetadata(
                name=_required_string(item, "name", f"Parameter {index}"),
                level_type=_required_string(item, "levelType", f"Parameter {index}"),
                level=level,
                unit=_required_string(item, "unit", f"Parameter {index}"),
                description=_required_string(item, "description", f"Parameter {index}"),
            )
        )
    return tuple(parameters)


def classify_relevant_parameters(
    parameters: Sequence[ParameterMetadata],
) -> Mapping[str, tuple[str, ...]]:
    available = {parameter.name for parameter in parameters}
    return {
        category: tuple(name for name in configured if name in available)
        for category, configured in RELEVANT_PARAMETER_NAMES.items()
    }


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise MesanInspectionError("times.json contains a non-string timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise MesanInspectionError(f"Invalid MESAN timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def analyze_times(payload: Mapping[str, Any]) -> TimeCoverage:
    values = payload.get("time")
    if not isinstance(values, list) or not values:
        raise MesanInspectionError("times.json has no timestamps")
    timestamps = sorted({_parse_timestamp(value) for value in values})
    spacing = tuple(
        sorted(
            {
                int((later - earlier).total_seconds())
                for earlier, later in pairwise(timestamps)
            }
        )
    )
    return TimeCoverage(
        first=timestamps[0],
        last=timestamps[-1],
        count=len(timestamps),
        spacing_seconds=spacing,
    )


def _coordinates(payload: Mapping[str, Any], context: str) -> tuple[str, list[Any]]:
    geometry = payload.get("geometry", payload)
    if not isinstance(geometry, dict):
        raise MesanInspectionError(f"{context} has no geometry object")
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if not isinstance(geometry_type, str) or not isinstance(coordinates, list):
        raise MesanInspectionError(f"{context} has invalid GeoJSON geometry")
    return geometry_type, coordinates


def _coordinate_pair(value: Any, context: str) -> tuple[float, float]:
    if not isinstance(value, list) or len(value) < 2:
        raise MesanInspectionError(f"{context} is not a coordinate pair")
    longitude, latitude = value[:2]
    if isinstance(longitude, bool) or isinstance(latitude, bool):
        raise MesanInspectionError(f"{context} has boolean coordinates")
    if not isinstance(longitude, (int, float)) or not isinstance(latitude, (int, float)):
        raise MesanInspectionError(f"{context} has non-numeric coordinates")
    return float(longitude), float(latitude)


def haversine_distance_m(
    first_longitude: float,
    first_latitude: float,
    second_longitude: float,
    second_latitude: float,
) -> float:
    radius_m = 6_371_008.8
    first_latitude_rad = math.radians(first_latitude)
    second_latitude_rad = math.radians(second_latitude)
    latitude_delta = second_latitude_rad - first_latitude_rad
    longitude_delta = math.radians(second_longitude - first_longitude)
    value = (
        math.sin(latitude_delta / 2) ** 2
        + math.cos(first_latitude_rad)
        * math.cos(second_latitude_rad)
        * math.sin(longitude_delta / 2) ** 2
    )
    return 2 * radius_m * math.asin(min(1.0, math.sqrt(value)))


def analyze_point(
    payload: Mapping[str, Any], latitude: float, longitude: float
) -> PointAnalysis:
    geometry_type, raw_coordinates = _coordinates(payload, "Point response")
    if geometry_type != "Point":
        raise MesanInspectionError(f"Point response geometry is {geometry_type!r}")
    returned_longitude, returned_latitude = _coordinate_pair(
        raw_coordinates, "Point response coordinate"
    )
    distance = haversine_distance_m(
        longitude,
        latitude,
        returned_longitude,
        returned_latitude,
    )
    return PointAnalysis(
        requested_longitude=longitude,
        requested_latitude=latitude,
        returned_longitude=returned_longitude,
        returned_latitude=returned_latitude,
        snapped=distance > 0.01,
        snap_distance_m=distance,
    )


def analyze_grid(
    payload: Mapping[str, Any], returned_longitude: float, returned_latitude: float
) -> GridAnalysis:
    geometry_type, raw_coordinates = _coordinates(payload, "Multipoint metadata")
    if geometry_type != "MultiPoint":
        raise MesanInspectionError(f"Multipoint geometry is {geometry_type!r}")
    returned_coordinate_present = False
    nearest_nonzero_distance = math.inf
    for index, raw_coordinate in enumerate(raw_coordinates):
        longitude, latitude = _coordinate_pair(
            raw_coordinate, f"Multipoint coordinate {index}"
        )
        distance = haversine_distance_m(
            returned_longitude,
            returned_latitude,
            longitude,
            latitude,
        )
        if distance <= 0.01:
            returned_coordinate_present = True
        elif distance < nearest_nonzero_distance:
            nearest_nonzero_distance = distance
    spacing = None if math.isinf(nearest_nonzero_distance) else nearest_nonzero_distance
    return GridAnalysis(
        geometry_type=geometry_type,
        coordinate_count=len(raw_coordinates),
        returned_coordinate_present=returned_coordinate_present,
        approximate_spacing_m=spacing,
    )


def assess_history(
    parameters: Sequence[ParameterMetadata], coverage: TimeCoverage
) -> tuple[HistoryAssessment, ...]:
    names = {parameter.name for parameter in parameters}
    hourly = coverage.is_uniform and coverage.timestep_seconds == 3600
    has_hourly_rain = "precipitation_amount_last_1_hours" in names
    has_temperature = "air_temperature" in names
    assessments: list[HistoryAssessment] = []
    for feature, days in RAIN_WINDOWS_DAYS.items():
        required_intervals = days * 24
        sufficient = hourly and has_hourly_rain and coverage.count >= required_intervals
        assessments.append(
            HistoryAssessment(
                feature=feature,
                sufficient=sufficient,
                reason=(
                    f"requires {required_intervals} hourly last-hour precipitation values; "
                    f"API exposes {coverage.count}"
                ),
            )
        )
    for feature, days in TEMPERATURE_WINDOWS_DAYS.items():
        required_samples = days * 24
        sufficient = hourly and has_temperature and coverage.count >= required_samples
        assessments.append(
            HistoryAssessment(
                feature=feature,
                sufficient=sufficient,
                reason=(
                    f"requires at least {required_samples} hourly air-temperature samples; "
                    f"API exposes {coverage.count}"
                ),
            )
        )
    assessments.extend(
        (
            HistoryAssessment(
                feature="days_since_significant_rain",
                sufficient=False,
                reason=(
                    "available history is limited to the current timestamp list; the "
                    "significant-rain threshold is not defined by MESAN metadata"
                ),
            ),
            HistoryAssessment(
                feature="dry_spell_length",
                sufficient=False,
                reason=(
                    "a dry spell can start before the first available timestamp, so its "
                    "length cannot be bounded by this history"
                ),
            ),
        )
    )
    return tuple(assessments)


def inspect_mesan(
    *,
    latitude: float = DEFAULT_LATITUDE,
    longitude: float = DEFAULT_LONGITUDE,
    base_url: str = BASE_URL,
    timeout: float = 60.0,
    opener: UrlOpener = urlopen,
) -> MesanInspection:
    client = SmhiMesanClient(base_url, timeout=timeout, opener=opener)
    created, parameters_payload, times, point_payload, multipoint = client.fetch_all(
        latitude, longitude
    )
    parameters = parse_parameters(parameters_payload)
    coverage = analyze_times(times)
    point = analyze_point(point_payload, latitude, longitude)
    time_series = point_payload.get("timeSeries")
    if not isinstance(time_series, list):
        raise MesanInspectionError("Point response has no timeSeries list")
    grid = analyze_grid(
        multipoint,
        point.returned_longitude,
        point.returned_latitude,
    )
    return MesanInspection(
        created_time=_required_string(created, "createdTime", "createdtime.json"),
        reference_time=_required_string(created, "referenceTime", "createdtime.json"),
        parameters=parameters,
        relevance=classify_relevant_parameters(parameters),
        time_coverage=coverage,
        point=point,
        point_time_series_count=len(time_series),
        grid=grid,
        history=assess_history(parameters, coverage),
        request_urls=tuple(client.request_urls),
    )


def _isoformat(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def print_report(report: MesanInspection) -> None:
    print("SMHI MESAN2G VERSION 3")
    print(f"\nCreated time: {report.created_time}")
    print(f"Reference time: {report.reference_time}")
    print(f"HTTP calls: {len(report.request_urls)}")

    print("\nPARAMETERS")
    print("name | levelType | level | unit")
    for parameter in report.parameters:
        print(
            f"{parameter.name} | {parameter.level_type} | "
            f"{parameter.level} | {parameter.unit}"
        )

    print("\nRELEVANT PARAMETERS (DIRECTLY FROM SMHI NAMES/DESCRIPTIONS)")
    for category, names in report.relevance.items():
        value = ", ".join(names) if names else "none advertised"
        print(f"{category}: {value}")

    coverage = report.time_coverage
    spacing = (
        f"{coverage.timestep_seconds / 3600:g} hour(s), uniform"
        if coverage.timestep_seconds is not None
        else f"non-uniform: {coverage.spacing_seconds} seconds"
    )
    print("\nTIME COVERAGE")
    print(f"First timestamp: {_isoformat(coverage.first)}")
    print(f"Last timestamp: {_isoformat(coverage.last)}")
    print(f"Timestamp count: {coverage.count}")
    print(f"Timestamp span: {coverage.timestamp_span_hours:g} hours")
    print(f"Timestep spacing: {spacing}")
    print(f"Point time-series count: {report.point_time_series_count}")

    point = report.point
    print("\nPOINT QUERY")
    print(
        f"Requested coordinate: {point.requested_latitude}, "
        f"{point.requested_longitude}"
    )
    print(
        f"Returned coordinate: {point.returned_latitude}, "
        f"{point.returned_longitude}"
    )
    print(f"Snapped to grid: {'yes' if point.snapped else 'no'}")
    print(f"Approximate snap distance: {point.snap_distance_m:.1f} m")

    grid = report.grid
    print("\nMULTIPOINT METADATA")
    print(f"Geometry type: {grid.geometry_type}")
    print(f"Coordinate count: {grid.coordinate_count}")
    print(
        "Returned point occurs in multipoint coordinates: "
        f"{'yes' if grid.returned_coordinate_present else 'no'}"
    )
    if grid.approximate_spacing_m is None:
        spacing_text = "unavailable"
    else:
        spacing_text = f"{grid.approximate_spacing_m / 1000:.3f} km"
    print(f"Approximate local grid spacing: {spacing_text}")
    print("No multipoint parameter/time data was requested.")

    print("\nHISTORICAL FEATURE SUFFICIENCY")
    for item in report.history:
        print(f"{item.feature}: {'yes' if item.sufficient else 'no'} — {item.reason}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect SMHI MESAN2G v3 metadata and one WGS84 point"
    )
    parser.add_argument("--latitude", type=float, default=DEFAULT_LATITUDE)
    parser.add_argument("--longitude", type=float, default=DEFAULT_LONGITUDE)
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--timeout", type=float, default=60.0)
    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = _parser().parse_args()
    try:
        report = inspect_mesan(
            latitude=args.latitude,
            longitude=args.longitude,
            base_url=args.base_url,
            timeout=args.timeout,
        )
    except (MesanInspectionError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
