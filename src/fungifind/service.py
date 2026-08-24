"""Application service: location -> features -> species scoring -> result."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as Date

from fungifind.data_sources.base import HabitatDataSource, WeatherDataSource
from fungifind.data_sources.mock import MockHabitatDataSource, MockWeatherDataSource
from fungifind.models import Location, ModelResult, Species
from fungifind.scoring import RuleBasedScoringEngine, ScoringEngine


def _parse_date(value: Date | str) -> Date:
    if isinstance(value, Date):
        return value
    try:
        return Date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"date must be an ISO date (YYYY-MM-DD), got {value!r}") from exc


@dataclass(slots=True)
class MushroomScoringService:
    habitat_source: HabitatDataSource
    weather_source: WeatherDataSource
    scoring_engine: ScoringEngine = field(default_factory=RuleBasedScoringEngine)

    def get_score(
        self,
        latitude: float,
        longitude: float,
        date: Date | str,
        species: Species | str,
    ) -> ModelResult:
        location = Location(latitude=latitude, longitude=longitude)
        target_date = _parse_date(date)
        parsed_species = Species.parse(species)
        habitat = self.habitat_source.get_features(location)
        weather = self.weather_source.get_features(location, target_date)
        return self.scoring_engine.score(
            species=parsed_species,
            location=location,
            target_date=target_date,
            habitat=habitat,
            weather=weather,
        )


_DEFAULT_SERVICE = MushroomScoringService(
    habitat_source=MockHabitatDataSource(),
    weather_source=MockWeatherDataSource(),
)


def get_mushroom_score(
    latitude: float,
    longitude: float,
    date: Date | str,
    species: Species | str,
) -> ModelResult:
    """Run the prototype with bundled synthetic data sources."""

    return _DEFAULT_SERVICE.get_score(latitude, longitude, date, species)

