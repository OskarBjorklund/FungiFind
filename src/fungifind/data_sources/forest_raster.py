"""Forest-share raster adapter and a conservative real/mock hybrid source."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, fields, replace
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar

from fungifind.data_sources.base import HabitatDataSource
from fungifind.data_sources.raster import RasterPointReader, RasterSample
from fungifind.models import (
    DataSourceMetadata,
    FeatureProvenance,
    FeatureSnapshot,
    Location,
    StaticHabitatFeatures,
)


class ForestShareInterpretation(str, Enum):
    """Semantic policy is explicit because a 0..100 range alone does not prove percent."""

    RAW_UNVALIDATED = "raw_unvalidated"
    PROVISIONAL_ZERO_TO_HUNDRED_SHARE = "provisional_zero_to_hundred_share"


class RasterValueInterpretationError(ValueError):
    """Raised when a value violates the explicitly selected semantic policy."""


TREE_PROFILE_FEATURES = (
    "spruce_fraction",
    "pine_fraction",
    "birch_fraction",
    "other_deciduous_fraction",
)

TREE_PROFILE_FILENAMES: Mapping[str, str] = {
    "spruce_fraction": "Gran_andel.tif",
    "pine_fraction": "Tall_andel.tif",
    "birch_fraction": "Bjork_andel.tif",
    "other_deciduous_fraction": "OvrLov_andel.tif",
}


@dataclass(frozen=True, slots=True)
class TreeFractionDiagnostics:
    """Diagnostics only: no value is rescaled or changed based on the sum."""

    tree_fraction_sum: float
    complete: bool
    is_near_one: bool
    is_clearly_below_one: bool
    exceeds_one: bool
    status: str
    missing_features: tuple[str, ...]
    nodata_features: tuple[str, ...]
    near_one_tolerance: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ForestProfileResult:
    snapshot: FeatureSnapshot[StaticHabitatFeatures]
    diagnostics: TreeFractionDiagnostics


def diagnose_tree_fractions(
    features: StaticHabitatFeatures,
    provenance: Mapping[str, FeatureProvenance] | None = None,
    *,
    near_one_tolerance: float = 0.05,
) -> TreeFractionDiagnostics:
    """Describe the unmodified sum of available profile fractions."""

    if not 0 <= near_one_tolerance < 1:
        raise ValueError("near_one_tolerance must be between 0 and 1")
    provenance = provenance or {}
    values: list[float] = []
    missing: list[str] = []
    nodata: list[str] = []
    for name in TREE_PROFILE_FEATURES:
        value = getattr(features, name)
        if value is None:
            missing.append(name)
            feature_provenance = provenance.get(name)
            if feature_provenance and feature_provenance.semantic_status == "nodata":
                nodata.append(name)
        else:
            values.append(value)

    fraction_sum = sum(values)
    complete = not missing
    is_near_one = complete and abs(fraction_sum - 1.0) <= near_one_tolerance
    is_clearly_below_one = complete and fraction_sum < 1.0 - near_one_tolerance
    exceeds_one = fraction_sum > 1.0
    if not complete:
        status = "incomplete"
    elif exceeds_one:
        status = "exceeds_one"
    elif is_near_one:
        status = "near_one"
    else:
        status = "clearly_below_one"
    return TreeFractionDiagnostics(
        tree_fraction_sum=fraction_sum,
        complete=complete,
        is_near_one=is_near_one,
        is_clearly_below_one=is_clearly_below_one,
        exceeds_one=exceeds_one,
        status=status,
        missing_features=tuple(missing),
        nodata_features=tuple(nodata),
        near_one_tolerance=near_one_tolerance,
    )


class ForestRasterDataSource:
    """Map one real forest-share raster to one StaticHabitatFeatures field."""

    _SUPPORTED_FEATURES: ClassVar[set[str]] = {
        "spruce_fraction",
        "pine_fraction",
        "birch_fraction",
        "other_deciduous_fraction",
        "beech_fraction",
        "oak_fraction",
        "other_tree_fraction",
    }

    def __init__(
        self,
        raster_path: str | Path,
        *,
        target_feature: str = "spruce_fraction",
        interpretation: ForestShareInterpretation | str = ForestShareInterpretation.RAW_UNVALIDATED,
        band: int = 1,
    ) -> None:
        if target_feature not in self._SUPPORTED_FEATURES:
            supported = ", ".join(sorted(self._SUPPORTED_FEATURES))
            raise ValueError(f"Unsupported forest feature {target_feature!r}; expected: {supported}")
        self.reader = RasterPointReader(raster_path, band=band)
        self.target_feature = target_feature
        self.interpretation = ForestShareInterpretation(interpretation)

    def get_features(self, location: Location) -> FeatureSnapshot[StaticHabitatFeatures]:
        sample = self.reader.sample(location)
        return self._snapshot_from_sample(sample)

    def get_features_many(
        self, locations: Sequence[Location]
    ) -> tuple[FeatureSnapshot[StaticHabitatFeatures], ...]:
        return tuple(
            self._snapshot_from_sample(sample)
            for sample in self.reader.sample_many(locations)
        )

    def _snapshot_from_sample(
        self, sample: RasterSample
    ) -> FeatureSnapshot[StaticHabitatFeatures]:
        interpreted_value: float | None = None
        semantic_status = "nodata"
        quality = 0.0
        if not sample.is_nodata:
            if self.interpretation is ForestShareInterpretation.RAW_UNVALIDATED:
                semantic_status = "raw_value_preserved_semantics_unvalidated"
                quality = 0.25
            else:
                if sample.value is None or not 0 <= sample.value <= 100:
                    raise RasterValueInterpretationError(
                        f"Raw value {sample.value!r} is outside the inspected 0..100 range"
                    )
                interpreted_value = float(sample.value) / 100.0
                semantic_status = (
                    "provisional_fraction_from_0_100_scale_not_semantically_validated"
                )
                quality = 0.60

        features = replace(StaticHabitatFeatures(), **{self.target_feature: interpreted_value})
        details: dict[str, str | float | int] = {
            "target_feature": self.target_feature,
            "interpretation": self.interpretation.value,
            "semantic_status": semantic_status,
            "pixel_row": sample.pixel_row,
            "pixel_col": sample.pixel_col,
            "source_epsg": sample.source_epsg or -1,
        }
        if sample.raw_value is not None:
            details["raw_value"] = sample.raw_value
        provenance = FeatureProvenance(
            source_name=f"forest_raster_{self.target_feature}",
            source_path=sample.source_path,
            quality=quality,
            is_mock=False,
            semantic_status=semantic_status,
            raw_value=sample.raw_value,
            interpreted_value=interpreted_value,
            is_nodata=sample.is_nodata,
            grid_signature=sample.grid_signature,
            details={
                "source_crs": sample.source_crs,
                "pixel_row": sample.pixel_row,
                "pixel_col": sample.pixel_col,
                "interpretation": self.interpretation.value,
            },
        )
        return FeatureSnapshot(
            features=features,
            metadata=DataSourceMetadata(
                source_name=f"forest_raster_{self.target_feature}",
                quality=quality,
                is_mock=False,
                details=details,
            ),
            feature_provenance={self.target_feature: provenance},
        )


class ForestProfileRasterDataSource:
    """Compose configured forest-share rasters using the same generic point reader."""

    # OvrLov is mapped to the aggregate other-deciduous feature requested by the
    # profile. Separate mock beech/oak/catch-all values are suppressed in hybrid
    # mode to avoid counting overlapping categories twice.
    fallback_exclusions: ClassVar[frozenset[str]] = frozenset(
        {"beech_fraction", "oak_fraction", "other_tree_fraction"}
    )

    def __init__(
        self,
        raster_layers: Mapping[str, str | Path],
        *,
        interpretation: ForestShareInterpretation | str = ForestShareInterpretation.RAW_UNVALIDATED,
    ) -> None:
        missing = sorted(set(TREE_PROFILE_FEATURES) - set(raster_layers))
        extra = sorted(set(raster_layers) - set(TREE_PROFILE_FEATURES))
        if missing or extra:
            raise ValueError(f"Expected tree profile layers {TREE_PROFILE_FEATURES}; missing={missing}, extra={extra}")
        self.interpretation = ForestShareInterpretation(interpretation)
        self.sources = {
            name: ForestRasterDataSource(
                raster_layers[name],
                target_feature=name,
                interpretation=self.interpretation,
            )
            for name in TREE_PROFILE_FEATURES
        }

    @classmethod
    def from_kind_directory(
        cls,
        data_directory: str | Path = "src/data/kind",
        *,
        interpretation: ForestShareInterpretation | str = ForestShareInterpretation.RAW_UNVALIDATED,
    ) -> ForestProfileRasterDataSource:
        directory = Path(data_directory)
        return cls(
            {name: directory / filename for name, filename in TREE_PROFILE_FILENAMES.items()},
            interpretation=interpretation,
        )

    def sample_profile(self, location: Location) -> ForestProfileResult:
        layers = {
            name: source.get_features(location)
            for name, source in self.sources.items()
        }
        return self._profile_from_layers(layers)

    def _profile_from_layers(
        self,
        layers: Mapping[str, FeatureSnapshot[StaticHabitatFeatures]],
    ) -> ForestProfileResult:
        feature_values: dict[str, float | None] = {}
        provenance: dict[str, FeatureProvenance] = {}
        source_crs_values: set[str] = set()
        pixel_values: set[tuple[int, int]] = set()
        grid_signatures: set[str] = set()
        qualities: list[float] = []
        for name, layer in layers.items():
            feature_values[name] = getattr(layer.features, name)
            feature_provenance = layer.feature_provenance[name]
            provenance[name] = feature_provenance
            source_crs_values.add(str(feature_provenance.details["source_crs"]))
            pixel_values.add(
                (
                    int(feature_provenance.details["pixel_row"]),
                    int(feature_provenance.details["pixel_col"]),
                )
            )
            if feature_provenance.grid_signature:
                grid_signatures.add(feature_provenance.grid_signature)
            qualities.append(feature_provenance.quality)

        features = StaticHabitatFeatures(**feature_values)
        diagnostics = diagnose_tree_fractions(features, provenance)
        snapshot = FeatureSnapshot(
            features=features,
            metadata=DataSourceMetadata(
                source_name="forest_tree_profile_rasters_v0",
                quality=min(qualities),
                is_mock=False,
                details={
                    "layer_count": len(self.sources),
                    "interpretation": self.interpretation.value,
                    "distinct_crs_count": len(source_crs_values),
                    "distinct_pixel_count": len(pixel_values),
                    "distinct_grid_count": len(grid_signatures),
                    "tree_fraction_sum": diagnostics.tree_fraction_sum,
                    "tree_fraction_status": diagnostics.status,
                },
            ),
            feature_provenance=provenance,
        )
        return ForestProfileResult(snapshot=snapshot, diagnostics=diagnostics)

    def get_features_many(
        self, locations: Sequence[Location]
    ) -> tuple[FeatureSnapshot[StaticHabitatFeatures], ...]:
        batches = {
            name: source.get_features_many(locations)
            for name, source in self.sources.items()
        }
        return tuple(
            self._profile_from_layers(
                {name: snapshots[index] for name, snapshots in batches.items()}
            ).snapshot
            for index in range(len(locations))
        )

    def get_features(self, location: Location) -> FeatureSnapshot[StaticHabitatFeatures]:
        return self.sample_profile(location).snapshot


class HybridHabitatDataSource:
    """Overlay available real fields on a fallback source, retaining per-field origin."""

    def __init__(
        self,
        real_source: HabitatDataSource,
        fallback_source: HabitatDataSource,
    ) -> None:
        self.real_source = real_source
        self.fallback_source = fallback_source
        self.fallback_exclusions = frozenset(
            getattr(real_source, "fallback_exclusions", frozenset())
        )

    def get_features(self, location: Location) -> FeatureSnapshot[StaticHabitatFeatures]:
        real = self.real_source.get_features(location)
        fallback = self.fallback_source.get_features(location)
        values: dict[str, object] = {}
        provenance: dict[str, FeatureProvenance] = {}
        real_count = 0
        mock_count = 0

        for feature_field in fields(StaticHabitatFeatures):
            name = feature_field.name
            real_value = getattr(real.features, name)
            fallback_value = getattr(fallback.features, name)
            if real_value is not None:
                values[name] = real_value
                real_count += 1
                provenance[name] = real.feature_provenance.get(
                    name,
                    FeatureProvenance(
                        source_name=real.metadata.source_name,
                        quality=real.metadata.quality,
                        is_mock=real.metadata.is_mock,
                        semantic_status="source_provided",
                    ),
                )
            else:
                if name in self.fallback_exclusions:
                    values[name] = None
                    real_candidate = real.feature_provenance.get(name)
                    if real_candidate is not None:
                        provenance[name] = real_candidate
                    continue
                values[name] = fallback_value
                if fallback_value is not None:
                    mock_count += 1
                    fallback_provenance = fallback.feature_provenance.get(
                        name,
                        FeatureProvenance(
                            source_name=fallback.metadata.source_name,
                            quality=fallback.metadata.quality,
                            is_mock=fallback.metadata.is_mock,
                            semantic_status="temporary_fallback",
                        ),
                    )
                    real_candidate = real.feature_provenance.get(name)
                    if real_candidate is not None:
                        candidate_details: dict[str, str | float | int] = {
                            **fallback_provenance.details,
                            "real_candidate_status": real_candidate.semantic_status,
                            "real_candidate_source": real_candidate.source_name,
                        }
                        if real_candidate.raw_value is not None:
                            candidate_details["real_candidate_raw_value"] = (
                                real_candidate.raw_value
                            )
                        fallback_provenance = replace(
                            fallback_provenance,
                            details=candidate_details,
                        )
                    provenance[name] = fallback_provenance
                else:
                    real_candidate = real.feature_provenance.get(name)
                    if real_candidate is not None:
                        provenance[name] = real_candidate

        # The existing confidence model has one habitat-source quality. Keep the
        # conservative fallback quality until scoring supports per-feature quality.
        return FeatureSnapshot(
            features=StaticHabitatFeatures(**values),
            metadata=DataSourceMetadata(
                source_name="hybrid_real_raster_and_mock_habitat_v0",
                quality=fallback.metadata.quality,
                is_mock=True,
                details={
                    "real_feature_count": real_count,
                    "mock_feature_count": mock_count,
                    "confidence_policy": "conservative_fallback_quality",
                },
            ),
            feature_provenance=provenance,
        )
