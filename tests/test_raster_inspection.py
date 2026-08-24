from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from fungifind.raster_inspection import InspectionOptions, discover_rasters, inspect_raster


@pytest.fixture
def synthetic_raster(tmp_path: Path) -> tuple[Path, np.ndarray]:
    raster_path = tmp_path / "tiny_forest.tif"
    values = np.arange(100, dtype=np.float32).reshape(10, 10)
    values[0, 0] = -9999.0
    values[1, 1] = -9999.0
    with rasterio.open(
        raster_path,
        "w",
        driver="GTiff",
        width=10,
        height=10,
        count=1,
        dtype="float32",
        crs="EPSG:3006",
        transform=from_origin(100_000, 7_000_000, 10, 10),
        nodata=-9999.0,
        compress="deflate",
    ) as dataset:
        dataset.write(values, 1)
        dataset.set_band_description(1, "synthetic values")
        dataset.set_band_unit(1, "test_unit")
        dataset.update_tags(source="pytest", meaning="intentionally unspecified")
    return raster_path, values


def test_exact_inspection_writes_complete_report_and_preview(
    synthetic_raster: tuple[Path, np.ndarray], tmp_path: Path
) -> None:
    raster_path, values = synthetic_raster
    output = tmp_path / "reports"

    report = inspect_raster(raster_path, output)
    statistics = report["statistics"]
    band = statistics["bands"][0]
    valid = values[values != -9999.0]

    assert report["epsg"] == 3006
    assert report["width"] == 10
    assert report["height"] == 10
    assert report["band_descriptions"] == ["synthetic values"]
    assert report["units"] == ["test_unit"]
    assert report["tags"]["default"]["source"] == "pytest"
    assert statistics["mode"] == "exact_streamed"
    assert statistics["are_approximate"] is False
    assert band["minimum"] == pytest.approx(float(valid.min()))
    assert band["maximum"] == pytest.approx(float(valid.max()))
    assert band["mean"] == pytest.approx(float(valid.mean()))
    assert band["standard_deviation"] == pytest.approx(float(valid.std()))
    assert band["nodata_percentage"] == pytest.approx(2.0)
    assert band["valid_percentage"] == pytest.approx(98.0)
    assert len(band["sample_of_valid_values"]) == 20
    assert (output / "tiny_forest.json").is_file()
    assert (output / "tiny_forest_preview.png").is_file()

    saved = json.loads((output / "tiny_forest.json").read_text(encoding="utf-8"))
    assert saved["statistics"]["bands"][0]["percentiles"]["50"] == pytest.approx(
        float(np.percentile(valid, 50))
    )


def test_forced_sampling_is_bounded_and_clearly_marked(
    synthetic_raster: tuple[Path, np.ndarray], tmp_path: Path
) -> None:
    raster_path, _ = synthetic_raster
    options = InspectionOptions(
        exact_scan_pixel_limit=10,
        max_sample_pixels=25,
        percentile_sample_size=25,
        preview_max_dimension=8,
    )

    report = inspect_raster(raster_path, tmp_path / "sampled", options)
    statistics = report["statistics"]

    assert statistics["mode"] == "approximate_downsampled"
    assert statistics["are_approximate"] is True
    assert statistics["approximate_fields"] == ["all_statistics"]
    assert statistics["sample_shape_per_band"] == {"height": 5, "width": 5}
    assert statistics["bands"][0]["examined_pixel_count"] <= 25
    assert statistics["bands"][0]["pixel_percentages_are_approximate"] is True
    assert max(report["preview"]["width"], report["preview"]["height"]) <= 8


def test_discover_rasters_accepts_file_and_directory(
    synthetic_raster: tuple[Path, np.ndarray], tmp_path: Path
) -> None:
    raster_path, _ = synthetic_raster
    (tmp_path / "ignore.txt").write_text("not a raster", encoding="utf-8")

    assert discover_rasters(raster_path) == [raster_path.resolve()]
    assert discover_rasters(tmp_path) == [raster_path.resolve()]


def test_integer_raster_reports_bounded_class_distribution(tmp_path: Path) -> None:
    raster_path = tmp_path / "classified.tif"
    values = np.array(
        [
            [1, 1, 2, 255],
            [1, 2, 2, 255],
            [3, 3, 4, 255],
            [1, 2, 3, 4],
        ],
        dtype=np.uint8,
    )
    with rasterio.open(
        raster_path,
        "w",
        driver="GTiff",
        width=4,
        height=4,
        count=1,
        dtype="uint8",
        crs="EPSG:3006",
        transform=from_origin(100_000, 7_000_000, 2, 2),
        nodata=255,
    ) as dataset:
        dataset.write(values, 1)

    exact = inspect_raster(raster_path, tmp_path / "exact")
    distribution = exact["statistics"]["bands"][0]["class_distribution"]

    assert distribution["status"] == "available"
    assert distribution["unique_valid_values"] == [1, 2, 3, 4]
    assert distribution["is_approximate"] is False
    assert {item["value"]: item["sample_count"] for item in distribution["classes"]} == {
        1: 4,
        2: 4,
        3: 3,
        4: 2,
    }

    sampled = inspect_raster(
        raster_path,
        tmp_path / "sampled_classes",
        InspectionOptions(exact_scan_pixel_limit=1, max_sample_pixels=4),
    )
    sampled_distribution = sampled["statistics"]["bands"][0]["class_distribution"]
    assert sampled_distribution["is_approximate"] is True
    assert sampled_distribution["valid_pixels_counted"] <= 4
