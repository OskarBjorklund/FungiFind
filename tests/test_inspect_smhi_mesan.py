from __future__ import annotations

import gzip
import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Self

import pytest

SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "inspect_smhi_mesan.py"
SPEC = importlib.util.spec_from_file_location("inspect_smhi_mesan", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
inspect_smhi_mesan = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = inspect_smhi_mesan
SPEC.loader.exec_module(inspect_smhi_mesan)


class _FakeResponse:
    def __init__(self, payload: dict[str, Any], *, gzip_encoded: bool = True) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.payload = gzip.compress(raw) if gzip_encoded else raw
        self.status = 200
        self.headers = {"Content-Encoding": "gzip" if gzip_encoded else ""}

    def read(self) -> bytes:
        return self.payload

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def _timestamps(count: int = 24) -> list[str]:
    first = datetime(2026, 8, 26, 12, tzinfo=UTC)
    return [
        (first + timedelta(hours=index)).isoformat().replace("+00:00", "Z")
        for index in reversed(range(count))
    ]


def _parameters() -> dict[str, Any]:
    def item(
        name: str,
        description: str,
        *,
        level_type: str = "hl",
        level: int = 0,
        unit: str = "unit",
    ) -> dict[str, Any]:
        return {
            "name": name,
            "description": description,
            "levelType": level_type,
            "level": level,
            "unit": unit,
        }

    return {
        "parameter": [
            item("air_temperature", "Air temperature at 2 metres height.", level=2),
            item("relative_humidity", "Relative humidity at 2 metres height.", level=2),
            item("wind_speed", "Wind speed at 10 metre.", level=10),
            item(
                "cloud_area_fraction",
                "Total Cloud Cover",
                level_type="entireAtmosphere",
                unit="oktas",
            ),
            item(
                "precipitation_amount_last_1_hours",
                "Precipitation amount last hour",
                unit="mm",
            ),
            item(
                "change_over_time_in_surface_snow_amount_1_hours",
                "Change over time in surface snow amount last hour",
                unit="cm",
            ),
            # The classifier must not infer radiation semantics from an unknown name.
            item("undocumented_flux", "Undocumented flux"),
        ]
    }


class _FakeOpener:
    def __init__(self) -> None:
        returned = [18.129709, 59.418897]
        self.payloads = {
            "createdtime.json": {
                "createdTime": "2026-08-27T11:39:46Z",
                "referenceTime": "2026-08-27T11:00:00Z",
            },
            "parameter.json": _parameters(),
            "times.json": {"time": _timestamps()},
            "geotype/point/lon/18.132/lat/59.412/data.json": {
                "geometry": {"type": "Point", "coordinates": returned},
                "timeSeries": [{"time": value, "data": {}} for value in _timestamps()],
            },
            "geotype/multipoint.json": {
                "type": "MultiPoint",
                "coordinates": [
                    returned,
                    [returned[0] + 0.045, returned[1]],
                    [returned[0] - 0.045, returned[1]],
                    [returned[0], returned[1] + 0.0225],
                    [returned[0], returned[1] - 0.0225],
                ],
            },
        }
        self.requests: list[tuple[str, str | None, float]] = []

    def __call__(self, request: Any, *, timeout: float) -> _FakeResponse:
        url = request.full_url
        self.requests.append((url, request.get_header("Accept-encoding"), timeout))
        key = next(key for key in self.payloads if url.endswith(key))
        return _FakeResponse(self.payloads[key])


def test_inspection_uses_exactly_five_small_metadata_or_point_calls() -> None:
    opener = _FakeOpener()

    report = inspect_smhi_mesan.inspect_mesan(
        base_url="https://example.test/api/category/mesan2g/version/3",
        opener=opener,
        timeout=12.0,
    )

    assert len(opener.requests) == 5
    assert [url.rsplit("/", 1)[-1] for url, _, _ in opener.requests] == [
        "createdtime.json",
        "parameter.json",
        "times.json",
        "data.json",
        "multipoint.json",
    ]
    assert all(encoding == "gzip" for _, encoding, _ in opener.requests)
    assert all(timeout == 12.0 for _, _, timeout in opener.requests)
    assert report.point_time_series_count == 24
    assert len(report.request_urls) == 5


def test_parameter_metadata_and_relevance_are_not_inferred() -> None:
    parameters = inspect_smhi_mesan.parse_parameters(_parameters())
    relevance = inspect_smhi_mesan.classify_relevant_parameters(parameters)

    assert parameters[0].name == "air_temperature"
    assert parameters[0].level_type == "hl"
    assert parameters[0].level == 2
    assert parameters[0].unit == "unit"
    assert relevance["air temperature"] == ("air_temperature",)
    assert relevance["precipitation"] == ("precipitation_amount_last_1_hours",)
    assert relevance["radiation"] == ()
    assert relevance["soil or surface"] == (
        "change_over_time_in_surface_snow_amount_1_hours",
    )


def test_time_range_and_history_assessment_distinguish_one_day_from_longer_windows() -> None:
    coverage = inspect_smhi_mesan.analyze_times({"time": _timestamps()})
    history = {
        item.feature: item
        for item in inspect_smhi_mesan.assess_history(
            inspect_smhi_mesan.parse_parameters(_parameters()), coverage
        )
    }

    assert coverage.first == datetime(2026, 8, 26, 12, tzinfo=UTC)
    assert coverage.last == datetime(2026, 8, 27, 11, tzinfo=UTC)
    assert coverage.count == 24
    assert coverage.timestamp_span_hours == 23
    assert coverage.timestep_seconds == 3600
    assert history["rain_1d"].sufficient is True
    for name in (
        "rain_3d",
        "rain_7d",
        "rain_14d",
        "rain_21d",
        "rain_30d",
        "temp_mean_3d",
        "temp_mean_7d",
        "temp_mean_14d",
        "days_since_significant_rain",
        "dry_spell_length",
    ):
        assert history[name].sufficient is False


def test_point_snapping_and_grid_metadata_are_reported() -> None:
    report = inspect_smhi_mesan.inspect_mesan(
        base_url="https://example.test",
        opener=_FakeOpener(),
    )

    assert report.point.snapped is True
    assert report.point.returned_latitude == 59.418897
    assert report.point.returned_longitude == 18.129709
    assert 700 < report.point.snap_distance_m < 900
    assert report.grid.geometry_type == "MultiPoint"
    assert report.grid.coordinate_count == 5
    assert report.grid.returned_coordinate_present is True
    assert report.grid.approximate_spacing_m is not None
    assert 2_000 < report.grid.approximate_spacing_m < 3_000


def test_non_gzip_json_is_supported_and_invalid_shape_is_rejected() -> None:
    client = inspect_smhi_mesan.SmhiMesanClient(
        "https://example.test",
        opener=lambda request, timeout: _FakeResponse({"time": []}, gzip_encoded=False),
    )
    assert client.get_json("times.json") == {"time": []}
    with pytest.raises(inspect_smhi_mesan.MesanInspectionError, match="no timestamps"):
        inspect_smhi_mesan.analyze_times({"time": []})

