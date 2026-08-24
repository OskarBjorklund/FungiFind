"""Data-source contracts and bundled synthetic adapters."""

from fungifind.data_sources.base import HabitatDataSource, WeatherDataSource
from fungifind.data_sources.forest_raster import (
    ForestProfileRasterDataSource,
    ForestProfileResult,
    ForestRasterDataSource,
    ForestShareInterpretation,
    HybridHabitatDataSource,
    TreeFractionDiagnostics,
    diagnose_tree_fractions,
)
from fungifind.data_sources.mock import MockHabitatDataSource, MockWeatherDataSource
from fungifind.data_sources.raster import RasterPointReader, RasterSample
from fungifind.data_sources.raster_features import (
    CompositeHabitatDataSource,
    ConfiguredRasterFeatureDataSource,
    GridAlignmentDiagnostics,
    RasterFeatureCollectionResult,
    RasterFeatureConfig,
    diagnose_grid_alignment,
)
from fungifind.data_sources.wetness_raster import (
    SLU_CLASSIFIED_WETNESS_LABELS,
    SLU_WETNESS_PRODUCT_DESCRIPTION,
    StaticWetnessClassMapping,
    StaticWetnessRasterDataSource,
    StaticWetnessResult,
)

__all__ = [
    "SLU_CLASSIFIED_WETNESS_LABELS",
    "SLU_WETNESS_PRODUCT_DESCRIPTION",
    "CompositeHabitatDataSource",
    "ConfiguredRasterFeatureDataSource",
    "ForestProfileRasterDataSource",
    "ForestProfileResult",
    "ForestRasterDataSource",
    "ForestShareInterpretation",
    "GridAlignmentDiagnostics",
    "HabitatDataSource",
    "HybridHabitatDataSource",
    "MockHabitatDataSource",
    "MockWeatherDataSource",
    "RasterFeatureCollectionResult",
    "RasterFeatureConfig",
    "RasterPointReader",
    "RasterSample",
    "StaticWetnessClassMapping",
    "StaticWetnessRasterDataSource",
    "StaticWetnessResult",
    "TreeFractionDiagnostics",
    "WeatherDataSource",
    "diagnose_grid_alignment",
    "diagnose_tree_fractions",
]
