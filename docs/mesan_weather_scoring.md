# MESAN history in dynamic fruiting scoring

## Data-source boundary

`MesanWeatherHistoryDataSource` implements the existing `WeatherDataSource`
protocol. A WGS84 coordinate is matched to the nearest grid point already stored
in `mesan_history.sqlite`. The match is rejected when it is more than 5 km away,
which prevents an unbackfilled location from silently reusing an unrelated point.

For a date, the adapter selects the latest stored UTC analysis on that date. For
a timezone-aware datetime, it selects the latest stored analysis at or before the
instant. It then calls the existing `get_weather_history_features`; no aggregate
formula is duplicated.

Only an aggregate whose `CoverageStatus` is `full` is copied into
`DynamicWeatherFeatures`. Partial and insufficient aggregates become `None` and
therefore reduce the existing scoring completeness/confidence rather than being
treated as complete observations. Per-feature provenance records coverage,
requested and snapped coordinates, target UTC hour, database path, source
products/versions, grid identities and sampling methods. Exact hourly URL/file
provenance remains on the SQLite rows referenced by that database path, grid and
time window.

Live MESAN2G v3 and historical MESAN GRIDPP may jointly cover one aggregate. The
database precedence remains:

```text
live MESAN2G v3 (priority 100)
    > historical MESAN GRIDPP feed 9 (priority 50)
```

No mock value is used to fill a partial real MESAN aggregate.

## Dynamic features

The scorer consumes these full-coverage fields:

```text
rainfall_1d_mm, rainfall_3d_mm
rainfall_7d_mm, rainfall_14d_mm
rainfall_21d_mm, rainfall_30d_mm
temp_mean_3d_c, temp_mean_7d_c, temp_mean_14d_c
relative_humidity_mean_3d_percent, relative_humidity_mean_7d_percent
```

`days_since_significant_rain` and `dry_spell_length_days` remain in the model and
provenance, but their default status is `threshold_required`. Both species have
`dry_spell_scoring_enabled=False`; no 1 mm/h or other biological threshold is
selected by the adapter or scorer.

## Transparent precipitation composition

The cumulative rain windows are correlated. They therefore do not enter the
fruiting score as six independent top-level components. Within each pair, the
score is a weighted mean of its two trapezoid-response scores:

| Species | `recent_rain` | `medium_term_rain` | `background_rain` |
|---|---|---|---|
| *C. cibarius* | 0.35 × 1d + 0.65 × 3d | 0.45 × 7d + 0.55 × 14d | 0.45 × 21d + 0.55 × 30d |
| *C. tubaeformis* | 0.30 × 1d + 0.70 × 3d | 0.40 × 7d + 0.60 × 14d | 0.40 × 21d + 0.60 × 30d |

The three groups then receive separate fruiting weights. Their combined weight is
0.45 for each species, but their distribution differs:

| Species | recent | medium | background |
|---|---:|---:|---:|
| *C. cibarius* | 0.10 | 0.22 | 0.13 |
| *C. tubaeformis* | 0.08 | 0.20 | 0.17 |

Every response uses `TrapezoidPreference(low_zero, low_optimal, high_optimal,
high_zero)`. All limits below are **preliminary biological assumptions** for a
transparent software prototype. They are neither calibrated nor field-validated.

### Rainfall curves, mm

| Window | *C. cibarius* | *C. tubaeformis* |
|---|---|---|
| 1d | (0, 2, 10, 30) | (0, 1.5, 10, 30) |
| 3d | (0, 4, 20, 55) | (0, 3, 22, 60) |
| 7d | (2, 14, 42, 90) | (3, 16, 48, 100) |
| 14d | (8, 30, 75, 150) | (10, 36, 90, 175) |
| 21d | (12, 42, 105, 210) | (16, 52, 130, 240) |
| 30d | (18, 55, 145, 280) | (22, 70, 175, 320) |

## Temperature and humidity

Temperature is a weighted mean of three non-linear window responses:

| Species | 3d weight/curve °C | 7d weight/curve °C | 14d weight/curve °C |
|---|---|---|---|
| *C. cibarius* | 0.20 / (4, 10, 18, 25) | 0.50 / (4, 9, 17, 24) | 0.30 / (3, 8, 16, 23) |
| *C. tubaeformis* | 0.15 / (-1, 5, 14, 21) | 0.45 / (-1, 5, 13.5, 20) | 0.40 / (-2, 4, 13, 19) |

Relative humidity has a deliberately smaller top-level weight than temperature
and total precipitation:

| Species | 3d weight/curve % | 7d weight/curve % | fruiting weight |
|---|---|---|---:|
| *C. cibarius* | 0.40 / (35, 70, 100, 100) | 0.60 / (35, 68, 100, 100) | 0.10 |
| *C. tubaeformis* | 0.35 / (40, 75, 100, 100) | 0.65 / (40, 73, 100, 100) | 0.12 |

Temperature weights are 0.30 and 0.28 respectively. Season retains weight 0.15.
All six top-level weights sum to one for each species.

## Static versus dynamic moisture

The validated SLU `static_wetness` class remains a separate habitat component. It
is long-term hydrological wetness potential and is not current soil moisture. A
future, explicitly separate `estimated_current_soil_moisture` may combine static
wetness with precipitation, temperature and drying processes. That physical model
is not implemented here.

## Reproducible local run

```powershell
python scripts/sample_mesan_model.py `
  --latitude 59.1699762 `
  --longitude 18.2500590 `
  --date 2026-08-27
```
