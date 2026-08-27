"""Typed point client for SMHI MESAN2G version 3 analyses."""

from __future__ import annotations

import gzip
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

MESAN2G_V3_BASE_URL = (
    "https://opendata-download-metanalys.smhi.se/api/category/mesan2g/version/3"
)
MESAN_SOURCE_NAME = "SMHI_MESAN2G"
MESAN_SOURCE_VERSION = "3"
USER_AGENT = "FungiFind-MESAN-client/0.1"

AIR_TEMPERATURE = "air_temperature"
PRECIPITATION_1H = "precipitation_amount_last_1_hours"
RELATIVE_HUMIDITY = "relative_humidity"
WIND_SPEED = "wind_speed"
REQUIRED_PARAMETERS = (
    AIR_TEMPERATURE,
    PRECIPITATION_1H,
    RELATIVE_HUMIDITY,
    WIND_SPEED,
)

JsonObject = dict[str, Any]
UrlOpener = Callable[..., Any]
ScalarValue = float | int | str | None


class MesanClientError(RuntimeError):
    """Raised for HTTP or schema errors in a MESAN point response."""


@dataclass(frozen=True, slots=True)
class MesanGridPoint:
    latitude: float
    longitude: float

    def __post_init__(self) -> None:
        if not -90 <= self.latitude <= 90:
            raise ValueError(f"latitude outside WGS84 bounds: {self.latitude}")
        if not -180 <= self.longitude <= 180:
            raise ValueError(f"longitude outside WGS84 bounds: {self.longitude}")

    @property
    def normalized_latitude(self) -> float:
        return round(self.latitude, 6)

    @property
    def normalized_longitude(self) -> float:
        return round(self.longitude, 6)


@dataclass(frozen=True, slots=True)
class MesanParameterMetadata:
    name: str
    level_type: str
    level: float | int
    unit: str
    description: str
    missing_value: float | int | None


@dataclass(frozen=True, slots=True)
class MesanHourlyAnalysis:
    """One hourly analysis at the grid coordinate returned by SMHI."""

    time: datetime
    grid_point: MesanGridPoint
    air_temperature_c: float | None
    precipitation_1h_mm: float | None
    relative_humidity_percent: float | None
    wind_speed_m_s: float | None
    values: Mapping[str, ScalarValue]
    units: Mapping[str, str]

    def __post_init__(self) -> None:
        if self.time.tzinfo is None:
            raise ValueError("MESAN timestep must have a timezone")
        if not math.isfinite(self.time.timestamp()):
            raise ValueError("MESAN timestep must be finite")
        for name in (
            "air_temperature_c",
            "precipitation_1h_mm",
            "relative_humidity_percent",
            "wind_speed_m_s",
        ):
            value = getattr(self, name)
            if value is not None and not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
        if self.precipitation_1h_mm is not None and self.precipitation_1h_mm < 0:
            raise ValueError("precipitation_1h_mm cannot be negative")
        if self.relative_humidity_percent is not None and not (
            0 <= self.relative_humidity_percent <= 100
        ):
            raise ValueError("relative_humidity_percent must be between 0 and 100")
        if self.wind_speed_m_s is not None and self.wind_speed_m_s < 0:
            raise ValueError("wind_speed_m_s cannot be negative")
        object.__setattr__(self, "time", self.time.astimezone(UTC))
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))
        object.__setattr__(self, "units", MappingProxyType(dict(self.units)))

    @property
    def is_complete(self) -> bool:
        return all(
            value is not None
            for value in (
                self.air_temperature_c,
                self.precipitation_1h_mm,
                self.relative_humidity_percent,
                self.wind_speed_m_s,
            )
        )


@dataclass(frozen=True, slots=True)
class MesanPointAnalysis:
    """One point response, retaining both requested and snapped coordinates."""

    requested_point: MesanGridPoint
    grid_point: MesanGridPoint
    reference_time: datetime
    created_time: datetime
    timesteps: tuple[MesanHourlyAnalysis, ...]
    parameters: Mapping[str, MesanParameterMetadata]
    source_api: str = MESAN_SOURCE_NAME
    source_version: str = MESAN_SOURCE_VERSION
    source_url: str = MESAN2G_V3_BASE_URL

    def __post_init__(self) -> None:
        if self.reference_time.tzinfo is None or self.created_time.tzinfo is None:
            raise ValueError("MESAN reference and created times must have timezones")
        object.__setattr__(self, "reference_time", self.reference_time.astimezone(UTC))
        object.__setattr__(self, "created_time", self.created_time.astimezone(UTC))
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))

    @property
    def is_snapped(self) -> bool:
        return (
            abs(self.requested_point.latitude - self.grid_point.latitude) > 1e-9
            or abs(self.requested_point.longitude - self.grid_point.longitude) > 1e-9
        )


def parse_utc_datetime(value: Any, context: str) -> datetime:
    if not isinstance(value, str):
        raise MesanClientError(f"{context} is not an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise MesanClientError(f"Invalid timestamp for {context}: {value!r}") from exc
    if parsed.tzinfo is None:
        raise MesanClientError(f"{context} has no timezone: {value!r}")
    return parsed.astimezone(UTC)


def format_utc_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("Datetime must have a timezone")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _required_string(payload: Mapping[str, Any], key: str, context: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise MesanClientError(f"{context} has no valid {key!r}")
    return value


def parse_parameter_metadata(
    payload: Mapping[str, Any],
) -> Mapping[str, MesanParameterMetadata]:
    raw_parameters = payload.get("parameter")
    if not isinstance(raw_parameters, list):
        raise MesanClientError("parameter.json has no parameter list")
    parameters: dict[str, MesanParameterMetadata] = {}
    for index, raw_parameter in enumerate(raw_parameters):
        if not isinstance(raw_parameter, dict):
            raise MesanClientError(f"Parameter {index} is not an object")
        name = _required_string(raw_parameter, "name", f"Parameter {index}")
        level = raw_parameter.get("level")
        if isinstance(level, bool) or not isinstance(level, (int, float)):
            raise MesanClientError(f"Parameter {name!r} has no numeric level")
        missing_value = raw_parameter.get("missingValue")
        if isinstance(missing_value, bool) or not isinstance(
            missing_value, (int, float, type(None))
        ):
            raise MesanClientError(f"Parameter {name!r} has invalid missingValue")
        if name in parameters:
            raise MesanClientError(f"Duplicate parameter metadata for {name!r}")
        parameters[name] = MesanParameterMetadata(
            name=name,
            level_type=_required_string(raw_parameter, "levelType", f"Parameter {name!r}"),
            level=level,
            unit=_required_string(raw_parameter, "unit", f"Parameter {name!r}"),
            description=_required_string(
                raw_parameter, "description", f"Parameter {name!r}"
            ),
            missing_value=missing_value,
        )
    missing = sorted(set(REQUIRED_PARAMETERS) - set(parameters))
    if missing:
        raise MesanClientError(f"MESAN metadata lacks required parameters: {missing}")
    return MappingProxyType(parameters)


def _numeric_value(
    value: Any, metadata: MesanParameterMetadata
) -> float | None:
    if value is None or value == metadata.missing_value:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def parse_point_analysis(
    payload: Mapping[str, Any],
    *,
    requested_point: MesanGridPoint,
    parameters: Mapping[str, MesanParameterMetadata],
    source_url: str = MESAN2G_V3_BASE_URL,
) -> MesanPointAnalysis:
    geometry = payload.get("geometry")
    if not isinstance(geometry, dict) or geometry.get("type") != "Point":
        raise MesanClientError("MESAN point response has no Point geometry")
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list) or len(coordinates) < 2:
        raise MesanClientError("MESAN point response has no coordinate pair")
    raw_longitude, raw_latitude = coordinates[:2]
    if (
        isinstance(raw_longitude, bool)
        or isinstance(raw_latitude, bool)
        or not isinstance(raw_longitude, (int, float))
        or not isinstance(raw_latitude, (int, float))
    ):
        raise MesanClientError("MESAN returned non-numeric point coordinates")
    grid_point = MesanGridPoint(float(raw_latitude), float(raw_longitude))

    raw_time_series = payload.get("timeSeries")
    if not isinstance(raw_time_series, list) or not raw_time_series:
        raise MesanClientError("MESAN point response has no non-empty timeSeries list")
    units = {name: metadata.unit for name, metadata in parameters.items()}
    timesteps: list[MesanHourlyAnalysis] = []
    seen_times: set[datetime] = set()
    for index, raw_timestep in enumerate(raw_time_series):
        if not isinstance(raw_timestep, dict):
            raise MesanClientError(f"MESAN timestep {index} is not an object")
        timestamp = parse_utc_datetime(raw_timestep.get("time"), f"timestep {index}")
        if timestamp in seen_times:
            raise MesanClientError(f"Duplicate MESAN timestep: {format_utc_datetime(timestamp)}")
        seen_times.add(timestamp)
        raw_values = raw_timestep.get("data")
        if not isinstance(raw_values, dict):
            raise MesanClientError(f"MESAN timestep {index} has no data object")

        preserved_values: dict[str, ScalarValue] = {}
        for name, value in raw_values.items():
            if not isinstance(name, str):
                continue
            preserved_values[name] = (
                value if isinstance(value, (int, float, str, type(None))) else None
            )
        timesteps.append(
            MesanHourlyAnalysis(
                time=timestamp,
                grid_point=grid_point,
                air_temperature_c=_numeric_value(
                    raw_values.get(AIR_TEMPERATURE), parameters[AIR_TEMPERATURE]
                ),
                precipitation_1h_mm=_numeric_value(
                    raw_values.get(PRECIPITATION_1H), parameters[PRECIPITATION_1H]
                ),
                relative_humidity_percent=_numeric_value(
                    raw_values.get(RELATIVE_HUMIDITY), parameters[RELATIVE_HUMIDITY]
                ),
                wind_speed_m_s=_numeric_value(
                    raw_values.get(WIND_SPEED), parameters[WIND_SPEED]
                ),
                values=preserved_values,
                units=units,
            )
        )
    timesteps.sort(key=lambda item: item.time)
    return MesanPointAnalysis(
        requested_point=requested_point,
        grid_point=grid_point,
        reference_time=parse_utc_datetime(payload.get("referenceTime"), "referenceTime"),
        created_time=parse_utc_datetime(payload.get("createdTime"), "createdTime"),
        timesteps=tuple(timesteps),
        parameters=parameters,
        source_url=source_url,
    )


class SmhiMesanPointClient:
    """Fetch parameter metadata and the current 24-hour MESAN point analysis."""

    def __init__(
        self,
        base_url: str = MESAN2G_V3_BASE_URL,
        *,
        timeout: float = 60.0,
        opener: UrlOpener = urlopen,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.opener = opener
        self._parameters: Mapping[str, MesanParameterMetadata] | None = None

    def _get_json(self, path: str) -> JsonObject:
        url = f"{self.base_url}/{path.lstrip('/')}"
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
                    raise MesanClientError(f"HTTP {status} for {url}")
                raw = response.read()
                content_encoding = response.headers.get("Content-Encoding", "")
        except HTTPError as exc:
            raise MesanClientError(f"HTTP {exc.code} for {url}: {exc.reason}") from exc
        except URLError as exc:
            raise MesanClientError(f"Could not reach {url}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise MesanClientError(f"Timed out requesting {url}") from exc
        if content_encoding.casefold() == "gzip":
            try:
                raw = gzip.decompress(raw)
            except gzip.BadGzipFile as exc:
                raise MesanClientError(f"Invalid gzip response from {url}") from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MesanClientError(f"Invalid JSON returned by {url}") from exc
        if not isinstance(payload, dict):
            raise MesanClientError(f"Expected a JSON object from {url}")
        return payload

    def get_parameters(self) -> Mapping[str, MesanParameterMetadata]:
        if self._parameters is None:
            self._parameters = parse_parameter_metadata(self._get_json("parameter.json"))
        return self._parameters

    def get_point_analysis(
        self, latitude: float, longitude: float
    ) -> MesanPointAnalysis:
        requested_point = MesanGridPoint(latitude, longitude)
        point_path = (
            f"geotype/point/lon/{format(longitude, '.12g')}"
            f"/lat/{format(latitude, '.12g')}/data.json"
        )
        return parse_point_analysis(
            self._get_json(point_path),
            requested_point=requested_point,
            parameters=self.get_parameters(),
            source_url=self.base_url,
        )
