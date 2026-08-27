"""Application service: location -> features -> species scoring -> result."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date as Date

from fungifind.data_sources.base import HabitatDataSource, WeatherDataSource
from fungifind.data_sources.mock import MockHabitatDataSource, MockWeatherDataSource
from fungifind.fruiting_v2 import ExperimentalFruitingV2Engine
from fungifind.models import Location, ModelResult, Species
from fungifind.moisture import CurrentSoilMoistureEstimator
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
    moisture_estimator: CurrentSoilMoistureEstimator = field(
        default_factory=CurrentSoilMoistureEstimator
    )
    fruiting_v2_engine: ExperimentalFruitingV2Engine = field(
        default_factory=ExperimentalFruitingV2Engine
    )

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
        scoring_result = self.scoring_engine.score(
            species=parsed_species,
            location=location,
            target_date=target_date,
            habitat=habitat,
            weather=weather,
        )
        moisture_result = self.moisture_estimator.estimate(habitat, weather)
        fruiting_v2 = self.fruiting_v2_engine.evaluate(
            species=parsed_species,
            target_date=target_date,
            weather=weather.features,
            moisture=moisture_result,
            production_result=scoring_result,
        )
        return replace(
            scoring_result,
            estimated_current_soil_moisture=(
                moisture_result.estimated_current_soil_moisture
            ),
            moisture_breakdown=moisture_result,
            fruiting_score_v2=fruiting_v2.fruiting_score_v2,
            final_score_v2=fruiting_v2.final_score_v2,
            fruiting_v2_breakdown=fruiting_v2,
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

