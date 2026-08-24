"""Configured raw raster features and composition of habitat data sources."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from fungifind.data_sources.base import HabitatDataSource
from fungifind.data_sources.raster import RasterPointReader, RasterSample
from fungifind.models import (
    DataSourceMetadata,
    FeatureProvenance,
    FeatureSnapshot,
    Location,
    StaticHabitatFeatures,
)


@dataclass(frozen=True, slots=True)
class RasterFeatureConfig:
    """Map one raster to a domain field without asserting source semantics."""

    target_feature: str
    label: str
    raster_path: str | Path
    band: int = 1


@dataclass(frozen=True, slots=True)
class GridAlignmentDiagnostics:
    status: str
    exact: bool
    distinct_grid_count: int
    missing_grid_features: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RasterFeatureCollectionResult:
    snapshot: FeatureSnapshot[StaticHabitatFeatures]
    samples: Mapping[str, RasterSample]
    grid_alignment: GridAlignmentDiagnostics


FOREST_STRUCTURE_FILENAMES: Mapping[str, tuple[str, str]] = {
    "forest_mean_height": ("HGV", "SGD2_HGV.tif"),
    "vegetation_ratio": ("Vegkvot", "SGD2_Vegkvot.tif"),
    "basal_area": ("GY", "SGD2_GY.tif"),
}


def diagnose_grid_alignment(
    provenance: Mapping[str, FeatureProvenance],
) -> GridAlignmentDiagnostics:
    signatures = {
        item.grid_signature for item in provenance.values() if item.grid_signature is not None
    }
    missing = tuple(
        name for name, item in provenance.items() if item.grid_signature is None
    )
    exact = bool(provenance) and len(signatures) == 1 and not missing
    if missing:
        status = "incomplete"
    elif exact:
        status = "exact"
    else:
        status = "different"
    return GridAlignmentDiagnostics(
        status=status,
        exact=exact,
        distinct_grid_count=len(signatures),
        missing_grid_features=missing,
    )


class ConfiguredRasterFeatureDataSource:
    """Read configured raw raster features with no semantic interpretation."""

    def __init__(self, configs: Sequence[RasterFeatureConfig]) -> None:
        if not configs:
            raise ValueError("At least one raster feature config is required")
        model_fields = {item.name for item in fields(StaticHabitatFeatures)}
        targets = [config.target_feature for config in configs]
        unknown = sorted(set(targets) - model_fields)
        if unknown:
            raise ValueError(f"Unknown StaticHabitatFeatures fields: {unknown}")
        if len(set(targets)) != len(targets):
            raise ValueError("Raster feature target names must be unique")
        self.configs = tuple(configs)
        self.readers = {
            config.target_feature: RasterPointReader(config.raster_path, band=config.band)
            for config in self.configs
        }

    @classmethod
    def forest_structure(
        cls,
        data_directory: str | Path = "src/data/misc_data",
    ) -> ConfiguredRasterFeatureDataSource:
        directory = Path(data_directory)
        return cls(
            [
                RasterFeatureConfig(
                    target_feature=name,
                    label=label,
                    raster_path=directory / filename,
                )
                for name, (label, filename) in FOREST_STRUCTURE_FILENAMES.items()
            ]
        )

    def sample_features(self, location: Location) -> RasterFeatureCollectionResult:
        samples: dict[str, RasterSample] = {}
        provenance: dict[str, FeatureProvenance] = {}
        feature_values: dict[str, None] = {}
        qualities: list[float] = []
        for config in self.configs:
            sample = self.readers[config.target_feature].sample(location)
            samples[config.target_feature] = sample
            quality = 0.0 if sample.is_nodata else 0.25
            qualities.append(quality)
            semantic_status = (
                "nodata" if sample.is_nodata else "raw_value_preserved_semantics_unvalidated"
            )
            # Domain values stay None until a unit and interpretation are validated.
            feature_values[config.target_feature] = None
            provenance[config.target_feature] = FeatureProvenance(
                source_name=f"configured_raster_{config.target_feature}",
                source_path=sample.source_path,
                quality=quality,
                is_mock=False,
                semantic_status=semantic_status,
                raw_value=sample.raw_value,
                interpreted_value=None,
                is_nodata=sample.is_nodata,
                grid_signature=sample.grid_signature,
                details={
                    "label": config.label,
                    "source_crs": sample.source_crs,
                    "source_epsg": sample.source_epsg or -1,
                    "pixel_row": sample.pixel_row,
                    "pixel_col": sample.pixel_col,
                    "unit": "unvalidated",
                },
            )

        alignment = diagnose_grid_alignment(provenance)
        snapshot = FeatureSnapshot(
            features=StaticHabitatFeatures(**feature_values),
            metadata=DataSourceMetadata(
                source_name="configured_forest_structure_rasters_v0",
                quality=min(qualities),
                is_mock=False,
                details={
                    "feature_count": len(self.configs),
                    "semantic_status": "unvalidated",
                    "grid_alignment": alignment.status,
                },
            ),
            feature_provenance=provenance,
        )
        return RasterFeatureCollectionResult(snapshot, samples, alignment)

    def get_features(self, location: Location) -> FeatureSnapshot[StaticHabitatFeatures]:
        return self.sample_features(location).snapshot


class CompositeHabitatDataSource:
    """Merge non-overlapping habitat snapshots without changing feature values."""

    def __init__(self, sources: Mapping[str, HabitatDataSource]) -> None:
        if not sources:
            raise ValueError("At least one habitat source is required")
        self.sources = dict(sources)
        self.fallback_exclusions = frozenset().union(
            *(
                frozenset(getattr(source, "fallback_exclusions", frozenset()))
                for source in self.sources.values()
            )
        )

    def get_features(self, location: Location) -> FeatureSnapshot[StaticHabitatFeatures]:
        values: dict[str, object] = {item.name: None for item in fields(StaticHabitatFeatures)}
        provenance: dict[str, FeatureProvenance] = {}
        qualities: list[float] = []
        contains_mock = False
        for source in self.sources.values():
            snapshot = source.get_features(location)
            qualities.append(snapshot.metadata.quality)
            contains_mock = contains_mock or snapshot.metadata.is_mock
            for feature_field in fields(StaticHabitatFeatures):
                name = feature_field.name
                incoming = getattr(snapshot.features, name)
                if incoming is not None:
                    if values[name] is not None:
                        raise ValueError(
                            f"Feature {name!r} is populated by more than one composite source"
                        )
                    values[name] = incoming
            for name, item in snapshot.feature_provenance.items():
                if name in provenance:
                    raise ValueError(
                        f"Feature provenance {name!r} occurs in more than one source"
                    )
                provenance[name] = item

        return FeatureSnapshot(
            features=StaticHabitatFeatures(**values),
            metadata=DataSourceMetadata(
                source_name="composite_habitat_sources_v0",
                quality=min(qualities),
                is_mock=contains_mock,
                details={
                    "source_count": len(self.sources),
                    "source_names": ",".join(self.sources),
                },
            ),
            feature_provenance=provenance,
        )
