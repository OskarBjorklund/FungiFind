"""Classified static-wetness raster source built on the generic point reader."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from fungifind.data_sources.raster import RasterPointReader, RasterSample
from fungifind.models import (
    DataSourceMetadata,
    FeatureProvenance,
    FeatureSnapshot,
    Location,
    StaticHabitatFeatures,
)

SLU_WETNESS_PRODUCT_DESCRIPTION = (
    "https://www.skogsstyrelsen.se/globalassets/sjalvservice/karttjanster/"
    "geodatatjanster/produktbeskrivningar/markfuktighetskarta-slu---produktbeskrivning.pdf"
)
SLU_CLASSIFIED_WETNESS_LABELS: Mapping[int, str] = {
    1: "torr-frisk",
    2: "frisk-fuktig",
    3: "fuktig-blöt",
    4: "öppet vatten",
}


@dataclass(frozen=True, slots=True)
class StaticWetnessClassMapping:
    """An explicit class mapping and the evidence that validates it."""

    labels: Mapping[int, str]
    source_reference: str
    semantic_status: str = "validated_class_mapping"

    def __post_init__(self) -> None:
        if not self.labels:
            raise ValueError("A validated static-wetness mapping cannot be empty")
        if not self.source_reference.strip():
            raise ValueError("A validated static-wetness mapping needs a source reference")
        if not self.semantic_status.strip():
            raise ValueError("A validated static-wetness mapping needs a semantic status")
        for class_value, label in self.labels.items():
            if isinstance(class_value, bool) or not isinstance(class_value, int):
                raise TypeError("Static-wetness mapping keys must be integer classes")
            if not label.strip():
                raise ValueError("Static-wetness class labels cannot be empty")
        object.__setattr__(self, "labels", MappingProxyType(dict(self.labels)))

    @classmethod
    def slu_classified(cls) -> StaticWetnessClassMapping:
        return cls(
            labels=SLU_CLASSIFIED_WETNESS_LABELS,
            source_reference=SLU_WETNESS_PRODUCT_DESCRIPTION,
            semantic_status="validated_official_class_mapping",
        )


@dataclass(frozen=True, slots=True)
class StaticWetnessResult:
    snapshot: FeatureSnapshot[StaticHabitatFeatures]
    sample: RasterSample


def _as_integer_class(value: float | None) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    integer = int(value)
    return integer if float(value) == integer else None


class StaticWetnessRasterDataSource:
    """Read one classified raster without conflating it with current soil moisture."""

    fallback_exclusions = frozenset({"static_wetness_class", "static_wetness_label"})

    def __init__(
        self,
        raster_path: str | Path,
        *,
        class_mapping: StaticWetnessClassMapping | None = None,
        band: int = 1,
        source_name: str = "classified_static_wetness_raster",
    ) -> None:
        self.reader = RasterPointReader(raster_path, band=band)
        self.class_mapping = class_mapping
        self.source_name = source_name

    @classmethod
    def slu_classified(
        cls,
        raster_path: str | Path = "src/data/misc_data/SLUMarkfuktighetKlassad.tif",
    ) -> StaticWetnessRasterDataSource:
        return cls(
            raster_path,
            class_mapping=StaticWetnessClassMapping.slu_classified(),
            source_name="slu_classified_static_wetness_raster",
        )

    def sample_wetness(self, location: Location) -> StaticWetnessResult:
        sample = self.reader.sample(location)
        raw_class = _as_integer_class(sample.value)
        interpreted_class: int | None = None
        interpreted_label: str | None = None

        if sample.is_nodata:
            semantic_status = "nodata"
            quality = 0.0
        elif raw_class is None:
            semantic_status = "raw_value_is_not_an_integer_class"
            quality = 0.0
        elif self.class_mapping is None:
            semantic_status = "raw_class_preserved_semantics_unvalidated"
            quality = 0.25
        elif raw_class not in self.class_mapping.labels:
            semantic_status = "unknown_class_not_in_validated_mapping"
            quality = 0.0
        else:
            interpreted_class = raw_class
            interpreted_label = self.class_mapping.labels[raw_class]
            semantic_status = self.class_mapping.semantic_status
            quality = 0.95

        details: dict[str, str | float | int] = {
            "source_file": Path(sample.source_path).name,
            "source_crs": sample.source_crs,
            "source_epsg": sample.source_epsg or -1,
            "pixel_row": sample.pixel_row,
            "pixel_col": sample.pixel_col,
            "temporal_meaning": "long_term_static_hydrological_wetness_potential",
            "dynamic_current_soil_moisture": "separate_feature_not_provided_here",
        }
        if sample.nodata_value is not None:
            details["nodata_value"] = sample.nodata_value
        if interpreted_label is not None:
            details["interpreted_class_label"] = interpreted_label
        if interpreted_class == 4:
            details["habitat_exclusion_code"] = "open_water"
            details["habitat_exclusion_label"] = (
                "Öppet vatten enligt klassad SLU-markfuktighetskarta"
            )
        if self.class_mapping is not None:
            details["class_mapping_source"] = self.class_mapping.source_reference

        provenance = FeatureProvenance(
            source_name=self.source_name,
            source_path=sample.source_path,
            quality=quality,
            is_mock=False,
            semantic_status=semantic_status,
            raw_value=sample.raw_value,
            interpreted_value=interpreted_class,
            is_nodata=sample.is_nodata,
            grid_signature=sample.grid_signature,
            details=details,
        )
        snapshot = FeatureSnapshot(
            features=StaticHabitatFeatures(
                static_wetness_class=interpreted_class,
                static_wetness_label=interpreted_label,
            ),
            metadata=DataSourceMetadata(
                source_name=self.source_name,
                quality=quality,
                is_mock=False,
                details={
                    "semantic_status": semantic_status,
                    "temporal_meaning": "long_term_static_hydrological_wetness_potential",
                },
            ),
            feature_provenance={
                "static_wetness_class": provenance,
                "static_wetness_label": provenance,
            },
        )
        return StaticWetnessResult(snapshot=snapshot, sample=sample)

    def get_features(self, location: Location) -> FeatureSnapshot[StaticHabitatFeatures]:
        return self.sample_wetness(location).snapshot
