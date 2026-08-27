"""Official SGU Jordarter 1:25 000--1:100 000 point sampling."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType

from fungifind.data_sources.vector import GeoPackageVectorPointReader, VectorPointSample
from fungifind.models import (
    DataSourceMetadata,
    FeatureProvenance,
    FeatureSnapshot,
    Location,
    StaticHabitatFeatures,
)

SGU_SOIL_PRODUCT_DESCRIPTION = (
    "https://resource.sgu.se/dokument/produkter/jordarter-25-100000-beskrivning.pdf"
)
DEFAULT_SGU_SOIL_GPKG = Path("src/data/soil/jordarter25k_100k.gpkg")
LEGACY_SGU_SOIL_GPKG = Path("src/data/soil_type/jordarter25k_100k.gpkg")
SGU_PRIMARY_SOIL_LAYER = "grundlager"
SGU_PRIMARY_CODE_FIELD = "jg2"
SGU_PRIMARY_LABEL_FIELD = "jg2_tx"

# Official value domain "Jordart grundlager (jg2)" in SGU's product description.
SGU_GRUNDLAGER_CLASS_LABELS: Mapping[int, str] = MappingProxyType(
    {
        1: "Mossetorv",
        5: "Kärrtorv",
        6: "Gyttja",
        9: "Svämsediment, ler--silt",
        10: "Svämsediment, sand",
        13: "Flygsand",
        16: "Gyttjelera (eller lergyttja)",
        17: "Postglacial lera",
        19: "Postglacial finlera",
        21: "Sand",
        22: "Postglacial grovlera",
        24: "Postglacial silt",
        26: "Finsand",
        28: "Postglacial finsand",
        31: "Postglacial sand",
        33: "Svallsediment, grus",
        34: "Klapper",
        36: "Skaljord",
        39: "Silt",
        40: "Glacial lera",
        43: "Glacial finlera",
        44: "Glacial grovlera",
        48: "Glacial silt",
        50: "Isälvssediment",
        51: "Isälvssediment, sten--block",
        55: "Isälvssediment, sand",
        57: "Isälvssediment, grus",
        62: "Svämsediment, grus",
        66: "Blockmark",
        75: "Torv",
        79: "Postglacial grovsilt-finsand",
        81: "Talus (rasmassor)",
        82: "Vittringsjord",
        85: "Lera",
        86: "Lera--silt",
        87: "Sand--grus",
        90: "Oklassat område",
        91: "Vatten",
        92: "Sten--block",
        93: "Grusig morän",
        95: "Sandig morän",
        97: "Sandig-siltig morän",
        98: "Morängrovlera",
        99: "Moränfinlera",
        100: "Morän",
        101: "Moränlera",
        200: "Fyllning",
        322: "Fyllning, rödfyr",
        823: "Fanerozoisk diabas",
        849: "Rösberg",
        850: "Sedimentärt berg",
        888: "Berg",
        890: "Urberg",
        1950: "Kalktuff",
        2306: "Bleke och kalkgyttja",
        2368: "Slamströmssediment, ler--block",
        2372: "Flytjord eller skredjord",
        8114: "Oklassat område, tidvis under vatten",
        8175: "Torv, tidvis under vatten",
        8186: "Lera--silt, tidvis under vatten",
        8802: "Älvsediment, grovsilt--finsand",
        8803: "Älvsediment, grus",
        8804: "Älvsediment",
        8806: "Älvsediment, ler--silt",
        8809: "Älvsediment, sand",
        8814: "Älvsediment sten--block",
        8919: "Vittringsjord, ler--silt",
        8937: "Svämsediment",
        8950: "Vittringsjord, sand--grus",
        9010: "Svämsediment, grovsilt--finsand",
        9060: "Glacial grovsilt--finsand",
        9147: "Morän omväxlande med sorterade sediment",
        9191: "Glaciär",
        9299: "Morän, sand",
        9336: "Morän, sten--block",
        9792: "Moränlera eller lerig morän",
        9794: "Lerig morän",
        9950: "Skålla av sedimentärt berg",
        9960: "Skålla av sandsten",
    }
)

# A separate, transparent interpretation of the official classes. It is retained
# only as a static feature in this step and is not converted to the scorer's legacy
# SoilType enum. Ambiguous sediments remain "other" rather than being guessed.
SGU_SOIL_GROUP_CODES: Mapping[str, frozenset[int]] = MappingProxyType(
    {
        "moraine": frozenset(
            {93, 95, 97, 98, 99, 100, 101, 9147, 9299, 9336, 9792, 9794}
        ),
        "organic_peat": frozenset({1, 5, 75, 8175}),
        "coarse_mineral": frozenset(
            {
                10,
                13,
                21,
                26,
                28,
                31,
                33,
                34,
                36,
                51,
                55,
                57,
                62,
                66,
                81,
                87,
                92,
                8803,
                8809,
                8814,
                8950,
            }
        ),
        "fine_mineral": frozenset(
            {
                9,
                17,
                19,
                22,
                24,
                39,
                40,
                43,
                44,
                48,
                79,
                85,
                86,
                8186,
                8802,
                8806,
                8919,
                9010,
                9060,
            }
        ),
        "bedrock_or_thin_soil": frozenset({823, 849, 850, 888, 890, 9950, 9960}),
        "water": frozenset({91}),
        "anthropogenic_fill": frozenset({200, 322}),
        "ice": frozenset({9191}),
        "other": frozenset(
            {6, 16, 50, 82, 90, 1950, 2306, 2368, 2372, 8114, 8804, 8937}
        ),
    }
)


def _build_group_mapping() -> Mapping[int, str]:
    grouped: dict[int, str] = {}
    for group, codes in SGU_SOIL_GROUP_CODES.items():
        duplicates = set(codes) & set(grouped)
        if duplicates:
            raise RuntimeError(f"SGU classes occur in multiple derived groups: {duplicates}")
        grouped.update({code: group for code in codes})
    missing = set(SGU_GRUNDLAGER_CLASS_LABELS) - set(grouped)
    unknown = set(grouped) - set(SGU_GRUNDLAGER_CLASS_LABELS)
    if missing or unknown:
        raise RuntimeError(f"Invalid SGU group coverage; missing={missing}, unknown={unknown}")
    return MappingProxyType(grouped)


SGU_SOIL_GROUP_BY_CODE: Mapping[int, str] = _build_group_mapping()


@dataclass(frozen=True, slots=True)
class SguSoilClassMapping:
    """Validated official code/label pairs plus a separate derived grouping."""

    labels: Mapping[int, str]
    groups: Mapping[int, str]
    source_reference: str
    semantic_status: str = "validated_official_sgu_jordarter_25k_100k_mapping"

    def __post_init__(self) -> None:
        labels = dict(self.labels)
        groups = dict(self.groups)
        if not labels or set(labels) != set(groups):
            raise ValueError("SGU labels and derived groups must cover exactly the same codes")
        if not self.source_reference.strip():
            raise ValueError("SGU class mapping needs an official source reference")
        object.__setattr__(self, "labels", MappingProxyType(labels))
        object.__setattr__(self, "groups", MappingProxyType(groups))

    @classmethod
    def official(cls) -> SguSoilClassMapping:
        return cls(
            labels=SGU_GRUNDLAGER_CLASS_LABELS,
            groups=SGU_SOIL_GROUP_BY_CODE,
            source_reference=SGU_SOIL_PRODUCT_DESCRIPTION,
        )


@dataclass(frozen=True, slots=True)
class SguSoilResult:
    snapshot: FeatureSnapshot[StaticHabitatFeatures]
    sample: VectorPointSample


def _resolve_default_source() -> Path:
    if DEFAULT_SGU_SOIL_GPKG.is_file():
        return DEFAULT_SGU_SOIL_GPKG
    if LEGACY_SGU_SOIL_GPKG.is_file():
        return LEGACY_SGU_SOIL_GPKG
    return DEFAULT_SGU_SOIL_GPKG


class SguSoilVectorDataSource:
    """Sample SGU's obligatory `grundlager` polygon layer using its RTree."""

    fallback_exclusions = frozenset({"soil_type_code", "soil_type_label", "soil_group"})

    def __init__(
        self,
        source_path: str | Path,
        *,
        class_mapping: SguSoilClassMapping | None = None,
        layer_name: str = SGU_PRIMARY_SOIL_LAYER,
        source_name: str = "sgu_jordarter_25k_100k",
    ) -> None:
        self.reader = GeoPackageVectorPointReader(
            source_path,
            layer_name,
            selected_attributes=(
                "jg2",
                "jg2_tx",
                "kartering",
                "karttyp",
                "symbol",
                "objectid",
                "geom_area",
                "geom_length",
            ),
        )
        self.class_mapping = class_mapping
        self.source_name = source_name

    @classmethod
    def official(
        cls, source_path: str | Path | None = None
    ) -> SguSoilVectorDataSource:
        resolved = _resolve_default_source() if source_path is None else Path(source_path)
        return cls(resolved, class_mapping=SguSoilClassMapping.official())

    def sample_soil(self, location: Location) -> SguSoilResult:
        sample = self.reader.sample(location)
        raw_code_value = sample.attributes.get(SGU_PRIMARY_CODE_FIELD)
        raw_label_value = sample.attributes.get(SGU_PRIMARY_LABEL_FIELD)
        raw_code = (
            int(raw_code_value)
            if isinstance(raw_code_value, int) and not isinstance(raw_code_value, bool)
            else None
        )
        raw_label = raw_label_value if isinstance(raw_label_value, str) else None

        soil_code: int | None = None
        soil_label: str | None = None
        soil_group: str | None = None
        if not sample.found:
            semantic_status = "no_feature_at_location"
            quality = 0.0
        elif raw_code is None or raw_label is None:
            semantic_status = "missing_or_invalid_official_code_or_label"
            quality = 0.0
        elif self.class_mapping is None:
            semantic_status = "raw_sgu_class_preserved_semantics_unvalidated"
            quality = 0.25
        elif raw_code not in self.class_mapping.labels:
            semantic_status = "unknown_class_not_in_validated_mapping"
            quality = 0.0
        elif self.class_mapping.labels[raw_code] != raw_label:
            semantic_status = "official_code_label_mismatch"
            quality = 0.0
        else:
            soil_code = raw_code
            soil_label = raw_label
            soil_group = self.class_mapping.groups[raw_code]
            semantic_status = self.class_mapping.semantic_status
            quality = 0.98

        raw_attributes = json.dumps(
            dict(sample.attributes), ensure_ascii=False, sort_keys=True, default=str
        )
        details: dict[str, str | float | int] = {
            "dataset": "SGU Jordarter 1:25 000--1:100 000",
            "source_file": Path(sample.source_path).name,
            "layer": sample.layer_name,
            "feature_id": sample.feature_id if sample.feature_id is not None else -1,
            "source_crs": sample.source_crs,
            "source_epsg": sample.source_epsg or -1,
            "projected_x": sample.projected_x,
            "projected_y": sample.projected_y,
            "raw_attributes_json": raw_attributes,
            "spatial_index_used": str(sample.spatial_index_used).lower(),
            "lookup_method": sample.lookup_method,
            "bbox_candidate_count": sample.candidate_count,
            "covering_feature_count": sample.matching_feature_count,
            "boundary_policy": "covers_including_boundary",
            "multiple_match_policy": "lowest_integer_fid",
        }
        if raw_code is not None:
            details["raw_class_code"] = raw_code
        if raw_label is not None:
            details["raw_official_label"] = raw_label
        if soil_group is not None:
            details["interpreted_soil_group"] = soil_group
        if self.class_mapping is not None:
            details["class_mapping_source"] = self.class_mapping.source_reference

        provenance = FeatureProvenance(
            source_name=self.source_name,
            source_path=sample.source_path,
            quality=quality,
            is_mock=False,
            semantic_status=semantic_status,
            raw_value=raw_code,
            interpreted_value=soil_code,
            is_nodata=False,
            details=details,
        )
        group_provenance = (
            replace(
                provenance,
                quality=0.95,
                semantic_status="derived_from_validated_official_sgu_class_mapping",
                interpreted_value=None,
                details={
                    **details,
                    "derived_mapping_status": "explicit_code_to_group_configuration",
                },
            )
            if soil_group is not None
            else provenance
        )
        snapshot = FeatureSnapshot(
            features=StaticHabitatFeatures(
                soil_type_code=soil_code,
                soil_type_label=soil_label,
                soil_group=soil_group,
            ),
            metadata=DataSourceMetadata(
                source_name=self.source_name,
                quality=quality,
                is_mock=False,
                details={
                    "semantic_status": semantic_status,
                    "layer": sample.layer_name,
                    "lookup_method": sample.lookup_method,
                },
            ),
            feature_provenance={
                "soil_type_code": provenance,
                "soil_type_label": provenance,
                "soil_group": group_provenance,
            },
        )
        return SguSoilResult(snapshot=snapshot, sample=sample)

    def get_features(self, location: Location) -> FeatureSnapshot[StaticHabitatFeatures]:
        return self.sample_soil(location).snapshot
