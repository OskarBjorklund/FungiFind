from __future__ import annotations

import gzip
import json
from typing import Any, Self

import pytest

from fungifind.data_sources.smhi_mesan import (
    MesanClientError,
    SmhiMesanPointClient,
)


def _parameters() -> dict[str, Any]:
    return {
        "parameter": [
            {
                "name": "air_temperature",
                "levelType": "hl",
                "level": 2,
                "unit": "Cel",
                "description": "Air temperature at 2 metres height.",
                "missingValue": 9999,
            },
            {
                "name": "precipitation_amount_last_1_hours",
                "levelType": "hl",
                "level": 0,
                "unit": "mm",
                "description": "Precipitation amount last hour",
                "missingValue": 9999,
            },
            {
                "name": "relative_humidity",
                "levelType": "hl",
                "level": 2,
                "unit": "percent",
                "description": "Relative humidity at 2 metres height.",
                "missingValue": 9999,
            },
            {
                "name": "wind_speed",
                "levelType": "hl",
                "level": 10,
                "unit": "m/s",
                "description": "Wind speed at 10 metre.",
                "missingValue": 9999,
            },
        ]
    }


def _point(*, missing_temperature: bool = False) -> dict[str, Any]:
    first_temperature = 9999 if missing_temperature else 15.5
    return {
        "createdTime": "2026-08-27T11:39:46Z",
        "referenceTime": "2026-08-27T11:00:00Z",
        "geometry": {"type": "Point", "coordinates": [18.129709, 59.418897]},
        "timeSeries": [
            {
                "time": "2026-08-27T11:00:00Z",
                "data": {
                    "air_temperature": 16.0,
                    "precipitation_amount_last_1_hours": 0.2,
                    "relative_humidity": 68,
                    "wind_speed": 2.1,
                    "cloud_area_fraction": 4,
                },
            },
            {
                "time": "2026-08-27T10:00:00Z",
                "data": {
                    "air_temperature": first_temperature,
                    "precipitation_amount_last_1_hours": 0.0,
                    "relative_humidity": 70,
                    "wind_speed": 1.9,
                },
            },
        ],
    }


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.status = 200
        self.headers = {"Content-Encoding": "gzip"}
        self._raw = gzip.compress(json.dumps(payload).encode())

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class _Opener:
    def __init__(self, point: dict[str, Any]) -> None:
        self.point = point
        self.urls: list[str] = []

    def __call__(self, request: Any, *, timeout: float) -> _Response:
        self.urls.append(request.full_url)
        if request.full_url.endswith("parameter.json"):
            return _Response(_parameters())
        return _Response(self.point)


def test_point_client_parses_typed_hours_units_and_snapped_coordinate() -> None:
    opener = _Opener(_point())
    client = SmhiMesanPointClient("https://example.test", opener=opener)

    result = client.get_point_analysis(59.412, 18.132)

    assert len(opener.urls) == 2
    assert result.requested_point.latitude == 59.412
    assert result.requested_point.longitude == 18.132
    assert result.grid_point.latitude == 59.418897
    assert result.grid_point.longitude == 18.129709
    assert result.is_snapped is True
    assert result.source_api == "SMHI_MESAN2G"
    assert result.source_version == "3"
    assert result.source_url == "https://example.test"
    assert [item.time.hour for item in result.timesteps] == [10, 11]
    first = result.timesteps[0]
    assert first.air_temperature_c == 15.5
    assert first.precipitation_1h_mm == 0.0
    assert first.relative_humidity_percent == 70.0
    assert first.wind_speed_m_s == 1.9
    assert first.units["air_temperature"] == "Cel"
    assert result.timesteps[1].values["cloud_area_fraction"] == 4


def test_missing_value_is_not_treated_as_a_real_temperature() -> None:
    result = SmhiMesanPointClient(
        "https://example.test", opener=_Opener(_point(missing_temperature=True))
    ).get_point_analysis(59.412, 18.132)

    assert result.timesteps[0].air_temperature_c is None
    assert result.timesteps[0].is_complete is False


@pytest.mark.parametrize(
    "mutation, message",
    [
        (lambda payload: payload.update(geometry={"type": "LineString"}), "Point geometry"),
        (
            lambda payload: payload["timeSeries"][0].update(time="not-a-time"),
            "Invalid timestamp",
        ),
        (
            lambda payload: payload["timeSeries"][1].update(
                time=payload["timeSeries"][0]["time"]
            ),
            "Duplicate MESAN timestep",
        ),
    ],
)
def test_malformed_point_response_is_rejected(
    mutation: Any, message: str
) -> None:
    payload = _point()
    mutation(payload)
    client = SmhiMesanPointClient("https://example.test", opener=_Opener(payload))

    with pytest.raises(MesanClientError, match=message):
        client.get_point_analysis(59.412, 18.132)
