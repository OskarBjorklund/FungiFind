# Estimated current soil moisture v1

## Meaning and boundary

`estimated_current_soil_moisture` is a transparent heuristic index from `0.0`
(relatively dry) to `1.0` (relatively wet). It describes the model's preliminary
estimate of current soil wetness at one location and date.

It is **not** volumetric soil-water content, field capacity, groundwater level,
a hydrological simulation, a mushroom probability, or a calibrated uncertainty
estimate. The parameters below are preliminary assumptions and have not been
calibrated against field measurements.

The estimator is implemented separately from `RuleBasedScoringEngine`. The
application service attaches its output to `ModelResult`, but it does not place
moisture in `factors` and does not change `habitat_score`, `fruiting_score`,
`final_score`, species weights, or fruiting logic.

## Accepted inputs

Only non-mock feature values with per-feature provenance whose semantic status
contains `validated` (but not `unvalidated`) are accepted.

| Group | Domain-model fields | Required for an estimate |
|---|---|---:|
| Static wetness | `static_wetness_class` | yes |
| Rain history | `rainfall_1d_mm`, `rainfall_3d_mm`, `rainfall_7d_mm`, `rainfall_14d_mm`, `rainfall_21d_mm`, `rainfall_30d_mm` | yes, all six |
| Temperature | `temp_mean_3d_c`, `temp_mean_7d_c`, `temp_mean_14d_c` | yes, all three |
| Relative humidity | `relative_humidity_mean_3d_percent`, `relative_humidity_mean_7d_percent` | yes, both |
| Soil | broad `soil_group` | no |
| Terrain | `slope_degrees` | no |

The domain model uses `rainfall_*` and a `_percent` suffix for RH; these are the
typed equivalents of the requested `rain_*` and `relative_humidity_mean_*`
features. Partial or insufficient MESAN aggregates are already represented as
`None`, so any incomplete central weather group produces status
`insufficient_central_inputs` and no final estimate. Missing soil or slope uses
the configured neutral retention `0.50` in the formula and reduces completeness.

Aspect, HGV, Vegkvot, GY, direct official SGU codes, legacy
`soil_moisture_index`, and the legacy dynamic
`estimated_current_soil_moisture_index` are not inputs.

## Components and exact formula

Every reported component and the final result is clamped or constructed in
`0..1`. Define:

```text
clamp(x) = min(max(x, 0), 1)
sat(rain, scale) = 1 - exp(-rain / scale)
```

### Baseline wetness

| SLU class | Verified meaning | `baseline_wetness` |
|---:|---|---:|
| 1 | torr-frisk | 0.30 |
| 2 | frisk-fuktig | 0.55 |
| 3 | fuktig-blöt | 0.80 |

Class 4 is open water and never receives an ordinary moisture estimate. A
validated habitat exclusion from any source also returns `excluded_habitat`.

### Rain recharge

Each cumulative window has a saturating response. The scale is the rainfall in
millimetres that produces `1 - exp(-1)`, about `0.632`, for that response.

| Window | Saturation scale, mm | Within-group weight |
|---|---:|---:|
| 1d | 8 | 0.40 recent |
| 3d | 15 | 0.60 recent |
| 7d | 28 | 0.45 medium |
| 14d | 48 | 0.55 medium |
| 21d | 70 | 0.45 background |
| 30d | 95 | 0.55 background |

```text
recent     = 0.40*sat(rain_1d, 8)  + 0.60*sat(rain_3d, 15)
medium     = 0.45*sat(rain_7d, 28) + 0.55*sat(rain_14d, 48)
background = 0.45*sat(rain_21d, 70) + 0.55*sat(rain_30d, 95)

rain_recharge = 0.50*recent + 0.30*medium + 0.20*background
```

The six overlapping windows therefore do not receive six independent top-level
weights. Recent history has the largest top-level effect, and arbitrarily large
rain approaches rather than exceeds `1.0`.

### Drying pressure

```text
weighted_temp = 0.50*temp_3d + 0.30*temp_7d + 0.20*temp_14d
temperature_drying = clamp((weighted_temp - 5°C) / (25°C - 5°C))

weighted_RH = 0.60*RH_3d + 0.40*RH_7d
RH_drying = 1 - clamp((weighted_RH - 40%) / (90% - 40%))

weather_drying = 0.55*temperature_drying + 0.45*RH_drying
drying_pressure = clamp(weather_drying * (1 - 0.65*rain_recharge))
```

Thus hot air and low RH increase drying. Low temperature, high RH, and recent
rain reduce it. The rain term is the v1 indirect representation of time since
rainfall; this is not a physical evapotranspiration calculation.

### Soil retention

| `soil_group` | `soil_retention` |
|---|---:|
| `organic_peat` | 0.95 |
| `fine_mineral` | 0.75 |
| `moraine` | 0.55 |
| `other` | 0.50 |
| `anthropogenic_fill` | 0.40 |
| `coarse_mineral` | 0.25 |
| `bedrock_or_thin_soil` | 0.15 |

These are preliminary hydrological directions, not measured retention
coefficients. `water` and `ice` are excluded; they are not assigned a normal
retention value.

### Terrain retention

```text
terrain_retention = 1 - clamp(slope_degrees / 30°)
```

Flat ground is `1.0`; slope at or above 30 degrees is `0.0`. Its final coefficient
is deliberately much smaller than static wetness and rain. Missing slope uses
neutral `0.50` and leaves `terrain_retention=None` in the breakdown.

### Final combination

Let missing optional soil or terrain retention be the neutral value `0.50`:

```text
raw = baseline_wetness
    + 0.35*rain_recharge
    - 0.30*drying_pressure
    + 0.16*(soil_retention - 0.50)
    + 0.06*(terrain_retention - 0.50)

estimated_current_soil_moisture = clamp(raw)
```

All coefficients, mappings, window weights, curve endpoints, saturation scales,
neutral values, and exclusion groups live in `CurrentSoilMoistureConfig`.

## Completeness, confidence, and provenance

Completeness is the sum of evidence weights whose complete, validated input group
was used:

| Input group | Completeness weight |
|---|---:|
| static wetness | 0.30 |
| rain history | 0.35 |
| temperature | 0.12 |
| relative humidity | 0.12 |
| soil | 0.08 |
| slope | 0.03 |

`confidence` is a data-quality-weighted completeness indicator, not statistical
confidence:

```text
confidence = sum(group_weight * minimum_feature_provenance_quality_in_group)
```

The result exposes six `used_*` flags, exact missing feature names, grouped source
names, completeness, confidence, status, estimator version, and all six model
components. Full per-feature provenance remains available on `ModelResult`.

## Real-point inspection (2026-08-27)

The reproducible run is stored in
`reports/current_soil_moisture_real_points.json` and can be regenerated with:

```powershell
python scripts/inspect_current_soil_moisture.py `
  --output reports/current_soil_moisture_real_points.json
```

| Point | SLU | Soil | Slope | Rain 3/7/14/30d mm | Temp 7d °C | RH 7d % | Baseline | Recharge | Drying | Soil ret. | Terrain ret. | Estimate | Confidence / completeness |
|---|---:|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| dry coarse mineral | 1 | coarse_mineral | — | 0.032 / 8.849 / 12.297 / 18.576 | 14.847 | 78.966 | 0.300 | 0.110948 | 0.356562 | 0.25 | — | 0.191863 | 0.9215 / 0.97 |
| mesic moraine | 2 | moraine | — | 0.032 / 8.849 / 12.297 / 18.576 | 14.847 | 78.966 | 0.550 | 0.110948 | 0.356562 | 0.55 | — | 0.489863 | 0.9215 / 0.97 |
| wet fine mineral | 3 | fine_mineral | — | 0.032 / 8.849 / 12.297 / 18.576 | 14.847 | 78.966 | 0.800 | 0.110948 | 0.356562 | 0.75 | — | 0.771863 | 0.9215 / 0.97 |
| peat | 2 | organic_peat | — | 0.032 / 8.849 / 12.297 / 18.576 | 14.847 | 78.966 | 0.550 | 0.110948 | 0.356562 | 0.95 | — | 0.553863 | 0.9215 / 0.97 |
| sloped bedrock | 1 | bedrock_or_thin_soil | 4.671 | 0.364 / 6.402 / 12.716 / 18.981 | 15.895 | 75.491 | 0.300 | 0.113574 | 0.403512 | 0.15 | 0.844297 | 0.183355 | 0.95 / 1.00 |

The ordering is qualitatively coherent for these deliberately contrasting
points: class 3/fine mineral is wettest; class 2/peat is above otherwise drier
class 2/moraine; class 1/coarse mineral and class 1/bedrock are driest. Four
points have no downloaded local DEM tile, so slope is correctly optional rather
than fabricated.

## Synthetic sensitivity results

Each comparison changes only the named factor; all other validated inputs stay
identical.

| Comparison | Baseline | Changed case | Result |
|---|---:|---:|---|
| More cumulative-consistent recent rain | moisture 0.644608; recharge 0.436779; drying 0.287547 | moisture 0.743421; recharge 0.667486; drying 0.227331 | moisture increased |
| Hotter and lower RH | moisture 0.644608; drying 0.287547 | moisture 0.532167; drying 0.662351 | moisture decreased |
| Peat versus coarse mineral | coarse 0.596608 | peat 0.708608 | peat is higher |
| Flat versus 30° slope | 30° 0.594608 | flat 0.654608 | steeper is lower |
| Very large rain in every window | — | recharge 1.000000 | saturates at 1 |
| Forced high/low raw formula | — | 1.000000 / 0.000000 | final clamp holds |

Automated tests also verify that missing optional inputs do not block the model,
missing or unvalidated central inputs do block it, water/ice/excluded habitat gets
no estimate, and service integration does not alter any scoring output.

## Limitations

- Weights and mappings are uncalibrated preliminary assumptions.
- Cumulative MESAN windows are correlated even after grouping.
- MESAN is grid weather, not an under-canopy microclimate measurement.
- Static wetness and soil classes have their own spatial resolution and mapping
  uncertainty.
- Snow, frozen ground, solar exposure/aspect, wind, canopy interception,
  evapotranspiration, drainage modifications, and antecedent seasonal state are
  absent.
- The heuristic is intended for inspection and later calibration. It must not be
  interpreted as field-measured moisture or used in fruiting scoring yet.
