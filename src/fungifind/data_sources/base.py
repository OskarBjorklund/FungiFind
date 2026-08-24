"""Interfaces implemented later by raster, API, database, or mock adapters."""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from fungifind.models import (
    DynamicWeatherFeatures,
    FeatureSnapshot,
    Location,
    StaticHabitatFeatures,
)


@runtime_checkable
class HabitatDataSource(Protocol):
    def get_features(self, location: Location) -> FeatureSnapshot[StaticHabitatFeatures]:
        """Return static features at a WGS84 location."""
        ...


@runtime_checkable
class WeatherDataSource(Protocol):
    def get_features(
        self, location: Location, target_date: date
    ) -> FeatureSnapshot[DynamicWeatherFeatures]:
        """Return weather aggregates ending at target_date."""
        ...

