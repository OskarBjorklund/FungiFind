"""Official NMD2023 v2.1 categorical land-cover sampling."""

from __future__ import annotations

import struct
from collections.abc import Mapping, Sequence
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

NMD2023_V2_1_PRODUCT_DESCRIPTION = (
    "https://geodata.naturvardsverket.se/nedladdning/marktacke/NMD2023/"
    "Basskikt_v2_x/NMD2023_Produktbeskrivning_Basskikt_NMD2023_v2_1.pdf"
)

# Official Swedish labels from the delivered .vat.dbf, cross-checked against
# Bilaga 1 in Naturvardsverket's NMD2023 basskikt v2.1 product description.
# Codes 412 and 413 are official v2.x classes but do not occur in the local VAT.
NMD2023_V2_1_CLASS_LABELS: Mapping[int, str] = MappingProxyType(
    {
        3: "Åkermark",
        23: "Låg fjällskog på våtmark",
        43: "Låg fjällskog på fastmark",
        51: "Byggnad",
        52: "Anlagd mark, ej byggnad eller väg/järnväg",
        53: "Väg eller järnväg",
        54: "Torvtäkt",
        61: "Inlandsvatten",
        62: "Hav",
        111: "Tallskog på fastmark",
        112: "Granskog på fastmark",
        113: "Barrblandskog på fastmark",
        114: "Lövblandad barrskog på fastmark",
        115: "Triviallövskog på fastmark",
        116: "Ädellövskog på fastmark",
        117: "Triviallövskog med ädellövinslag på fastmark",
        118: "Temporärt ej skog på fastmark",
        121: "Tallskog på våtmark",
        122: "Granskog på våtmark",
        123: "Barrblandskog på våtmark",
        124: "Lövblandad barrskog på våtmark",
        125: "Triviallövskog på våtmark",
        126: "Ädellövskog på våtmark",
        127: "Triviallövskog med ädellövinslag på våtmark",
        128: "Temporärt ej skog på våtmark",
        200: "Öppen våtmark (underindelning saknas)",
        211: "Buskmyr",
        212: "Ristuvemyr",
        213: "Fastmattemyr, mager",
        214: "Fastmattemyr, frodig",
        215: "Sumpkärr",
        216: "Mjukmattemyr",
        217: "Lösbottenmyr",
        218: "Övrig öppen myr",
        221: "Våtmark med buskar",
        222: "Risdominerad våtmark",
        223: "Gräsdominerad våtmark, mager",
        224: "Gräsdominerad våtmark, frodvuxen",
        225: "Gräsdominerad våtmark, högvuxen",
        226: "Mossdominerad våtmark",
        227: "Våtmark utan växttäcke",
        228: "Övrig öppen våtmark",
        411: "Öppen fastmark utan vegetation (ej glaciär eller varaktigt snöfält)",
        412: "Glaciär",
        413: "Varaktigt snöfält",
        4211: "Torr buskdominerad mark",
        4212: "Frisk buskdominerad mark",
        4213: "Frisk-fuktig buskdominerad mark",
        4221: "Torr risdominerad mark",
        4222: "Frisk risdominerad mark",
        4223: "Frisk-fuktig risdominerad mark",
        4231: "Torr gräsdominerad mark",
        4232: "Frisk gräsdominerad mark",
        4233: "Frisk-fuktig gräsdominerad mark",
    }
)

# These exclusions are categorical eligibility rules, not preference weights.
# All are direct consequences of official NMD class definitions. Open wetland,
# wetland forest, and other natural/open land are deliberately absent.
NMD2023_V2_1_EXCLUSION_RULES: Mapping[int, tuple[str, str]] = MappingProxyType(
    {
        3: ("agricultural_land", "Åkermark enligt NMD"),
        51: ("built_or_artificial_land", "Byggnad enligt NMD"),
        52: ("built_or_artificial_land", "Anlagd mark enligt NMD"),
        53: ("built_or_artificial_land", "Väg eller järnväg enligt NMD"),
        54: ("built_or_artificial_land", "Torvtäkt enligt NMD"),
        61: ("open_water", "Inlandsvatten enligt NMD"),
        62: ("open_water", "Hav enligt NMD"),
        412: ("permanent_ice_or_snow", "Glaciär enligt NMD"),
        413: ("permanent_ice_or_snow", "Varaktigt snöfält enligt NMD"),
    }
)

DEFAULT_NMD2023_RASTER = Path("src/data/landcover/NMD2023bas_v2_1.tif")
LEGACY_NMD2023_RASTER = Path("src/data/base_layer/NMD2023bas_v2_1.tif")


class NmdValueTableError(ValueError):
    """Raised when a local Esri VAT cannot validate the bundled mapping."""


def read_esri_vat_labels(path: str | Path) -> dict[int, str]:
    """Read the tiny Value/Klass columns from a dBase VAT without extra dependencies."""

    vat_path = Path(path)
    payload = vat_path.read_bytes()
    if len(payload) < 33:
        raise NmdValueTableError(f"VAT is too short to contain a dBase header: {vat_path}")
    record_count = struct.unpack("<I", payload[4:8])[0]
    header_length = struct.unpack("<H", payload[8:10])[0]
    record_length = struct.unpack("<H", payload[10:12])[0]

    fields: list[tuple[str, int, int]] = []
    offset = 1
    descriptor_offset = 32
    while descriptor_offset < header_length:
        if payload[descriptor_offset] == 0x0D:
            break
        descriptor = payload[descriptor_offset : descriptor_offset + 32]
        name = descriptor[:11].split(b"\0", 1)[0].decode("ascii")
        length = descriptor[16]
        fields.append((name, offset, length))
        offset += length
        descriptor_offset += 32

    by_name = {name.casefold(): (field_offset, length) for name, field_offset, length in fields}
    if "value" not in by_name or "klass" not in by_name:
        raise NmdValueTableError(f"VAT must contain Value and Klass fields: {vat_path}")

    value_offset, value_length = by_name["value"]
    label_offset, label_length = by_name["klass"]
    labels: dict[int, str] = {}
    for index in range(record_count):
        start = header_length + index * record_length
        record = payload[start : start + record_length]
        if len(record) != record_length:
            raise NmdValueTableError(f"VAT contains an incomplete record: {vat_path}")
        if record[0] == 0x2A:  # dBase deletion marker
            continue
        value_text = record[value_offset : value_offset + value_length].decode("ascii").strip()
        label_bytes = record[label_offset : label_offset + label_length].rstrip(b" \0")
        if not value_text or not label_bytes:
            continue
        try:
            label = label_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise NmdValueTableError(f"VAT labels are not UTF-8: {vat_path}") from exc
        labels[int(value_text)] = label
    return labels


@dataclass(frozen=True, slots=True)
class NmdLandcoverClassMapping:
    """An explicit NMD mapping with optional verification against the delivered VAT."""

    labels: Mapping[int, str]
    exclusion_rules: Mapping[int, tuple[str, str]]
    source_reference: str
    semantic_status: str = "validated_official_nmd_class_mapping"
    local_value_table: str | None = None

    def __post_init__(self) -> None:
        labels = dict(self.labels)
        exclusion_rules = dict(self.exclusion_rules)
        if not labels:
            raise ValueError("An NMD class mapping cannot be empty")
        if not self.source_reference.strip():
            raise ValueError("An NMD class mapping needs a source reference")
        for raw_class, label in labels.items():
            if isinstance(raw_class, bool) or not isinstance(raw_class, int):
                raise TypeError("NMD mapping keys must be integer classes")
            if not label.strip():
                raise ValueError("NMD class labels cannot be empty")
        unknown_exclusions = sorted(set(exclusion_rules) - set(labels))
        if unknown_exclusions:
            raise ValueError(f"NMD exclusions refer to unknown classes: {unknown_exclusions}")
        if self.local_value_table is not None:
            delivered = read_esri_vat_labels(self.local_value_table)
            mismatches = {
                raw_class: (label, labels.get(raw_class))
                for raw_class, label in delivered.items()
                if labels.get(raw_class) != label
            }
            if mismatches:
                raise NmdValueTableError(
                    f"Official mapping disagrees with delivered VAT: {mismatches}"
                )
        object.__setattr__(self, "labels", MappingProxyType(labels))
        object.__setattr__(self, "exclusion_rules", MappingProxyType(exclusion_rules))

    @classmethod
    def official_v2_1(cls, value_table: str | Path | None = None) -> NmdLandcoverClassMapping:
        return cls(
            labels=NMD2023_V2_1_CLASS_LABELS,
            exclusion_rules=NMD2023_V2_1_EXCLUSION_RULES,
            source_reference=NMD2023_V2_1_PRODUCT_DESCRIPTION,
            semantic_status="validated_official_nmd2023_v2_1_class_mapping",
            local_value_table=None if value_table is None else str(Path(value_table).resolve()),
        )


@dataclass(frozen=True, slots=True)
class LandcoverResult:
    snapshot: FeatureSnapshot[StaticHabitatFeatures]
    sample: RasterSample
    exclusion_reason: tuple[str, str] | None


def _as_integer_class(value: float | None) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    integer = int(value)
    return integer if float(value) == integer else None


def _resolve_default_raster() -> Path:
    if DEFAULT_NMD2023_RASTER.is_file():
        return DEFAULT_NMD2023_RASTER
    if LEGACY_NMD2023_RASTER.is_file():
        return LEGACY_NMD2023_RASTER
    return DEFAULT_NMD2023_RASTER


class NmdLandcoverRasterDataSource:
    """Read a categorical NMD raster through the shared RasterPointReader."""

    fallback_exclusions = frozenset({"landcover_class", "landcover_label"})

    def __init__(
        self,
        raster_path: str | Path,
        *,
        class_mapping: NmdLandcoverClassMapping | None = None,
        band: int = 1,
        source_name: str = "categorical_landcover_raster",
    ) -> None:
        self.reader = RasterPointReader(raster_path, band=band)
        self.class_mapping = class_mapping
        self.source_name = source_name

    @classmethod
    def nmd2023_v2_1(cls, raster_path: str | Path | None = None) -> NmdLandcoverRasterDataSource:
        resolved = _resolve_default_raster() if raster_path is None else Path(raster_path)
        value_table = Path(f"{resolved}.vat.dbf")
        mapping = NmdLandcoverClassMapping.official_v2_1(
            value_table if value_table.is_file() else None
        )
        return cls(
            resolved,
            class_mapping=mapping,
            source_name="naturvardsverket_nmd2023_basskikt_v2_1",
        )

    def sample_landcover(self, location: Location) -> LandcoverResult:
        sample = self.reader.sample(location)
        return self._result_from_sample(sample)

    def _result_from_sample(self, sample: RasterSample) -> LandcoverResult:
        raw_class = _as_integer_class(sample.value)
        interpreted_class: int | None = None
        interpreted_label: str | None = None
        exclusion_reason: tuple[str, str] | None = None

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
            exclusion_reason = self.class_mapping.exclusion_rules.get(raw_class)
            semantic_status = self.class_mapping.semantic_status
            quality = 0.98

        details: dict[str, str | float | int] = {
            "source_file": Path(sample.source_path).name,
            "source_crs": sample.source_crs,
            "source_epsg": sample.source_epsg or -1,
            "pixel_row": sample.pixel_row,
            "pixel_col": sample.pixel_col,
            "product": "NMD2023_basskikt_v2_1",
            "searchable_habitat": (
                "unknown"
                if interpreted_class is None
                else ("no" if exclusion_reason is not None else "yes")
            ),
        }
        if sample.nodata_value is not None:
            details["nodata_value"] = sample.nodata_value
        if interpreted_label is not None:
            details["official_class_label"] = interpreted_label
        if self.class_mapping is not None:
            details["class_mapping_source"] = self.class_mapping.source_reference
            if self.class_mapping.local_value_table is not None:
                details["local_value_table"] = self.class_mapping.local_value_table
        if exclusion_reason is not None:
            details["habitat_exclusion_code"] = exclusion_reason[0]
            details["habitat_exclusion_label"] = exclusion_reason[1]

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
                landcover_class=interpreted_class,
                landcover_label=interpreted_label,
            ),
            metadata=DataSourceMetadata(
                source_name=self.source_name,
                quality=quality,
                is_mock=False,
                details={
                    "semantic_status": semantic_status,
                    "product": "NMD2023_basskikt_v2_1",
                },
            ),
            feature_provenance={
                "landcover_class": provenance,
                "landcover_label": provenance,
            },
        )
        return LandcoverResult(
            snapshot=snapshot,
            sample=sample,
            exclusion_reason=exclusion_reason,
        )

    def get_features(self, location: Location) -> FeatureSnapshot[StaticHabitatFeatures]:
        return self.sample_landcover(location).snapshot

    def get_features_many(
        self, locations: Sequence[Location]
    ) -> tuple[FeatureSnapshot[StaticHabitatFeatures], ...]:
        return tuple(
            self._result_from_sample(sample).snapshot
            for sample in self.reader.sample_many(locations)
        )
