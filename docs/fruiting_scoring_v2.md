# Experimental fruiting scoring v2

## Status and purpose

`fruiting_score_v2` and `final_score_v2` are parallel experimental outputs. The
production `habitat_score`, `fruiting_score`, and `final_score` still come from
the unchanged `RuleBasedScoringEngine`; v2 is attached afterwards by the
application service.

V2 tests whether the separately estimated current soil moisture can replace the
direct medium/background-rain and relative-humidity components in dynamic
fruiting scoring. All weights and curve endpoints are preliminary biological
assumptions. None has been calibrated against mushroom observations.

## Dependency map and double-counting boundary

```text
rain 1/3/7/14/21/30 ───────┐
temperature 3/7/14 ────────┼──> current-soil-moisture estimator
RH 3/7 ─────────────────────┘              │
                                           v
                                  moisture preference ─┐

temperature 3/7/14 ──> biological temperature ────────┤
season/day-of-year ───> phenology/season ──────────────┼──> fruiting v2
rain 1/3 ─────────────> small recent-rain trigger ─────┘
```

There is no `medium_term_rain`, `background_rain`, `precipitation`, or
`relative_humidity` top-level component in v2. Rain 7/14/21/30 and RH influence
v2 only through moisture.

Rain 1/3 is intentionally reused as a trigger with weight `0.10`: current soil
wetness and a recent fruiting stimulus are treated as different mechanisms.
Temperature is also intentionally reused: it affects moisture through drying
and fruiting through a separate species-specific biological response.

The unchanged habitat score includes static SLU wetness while moisture also uses
that class. Consequently `final_score_v2` still has some static-wetness reuse
across the habitat and fruiting domains. This experiment does not redesign the
habitat score; that remaining dependency must be considered before promotion.

## Exact v2 formula

For species `s`, define:

```text
M_s = species moisture preference(estimated_current_soil_moisture)
T_s = existing species temperature component
S_s = existing species season component
R_s = existing species recent-rain response using only rain 1d and 3d
```

```text
Cantharellus cibarius:
fruiting_score_v2 = 0.45*M + 0.25*T + 0.20*S + 0.10*R
final_score_v2    = 0.60*habitat_score + 0.40*fruiting_score_v2

Craterellus tubaeformis:
fruiting_score_v2 = 0.50*M + 0.22*T + 0.18*S + 0.10*R
final_score_v2    = 0.55*habitat_score + 0.45*fruiting_score_v2
```

All components are bounded in `0..1`; weights sum to one. V2 uses the same final
habitat/fruiting domain split as production v1 so the experimental difference is
isolated to dynamic fruiting composition.

## Species moisture curves

Both use the existing transparent
`TrapezoidPreference(low_zero, low_optimal, high_optimal, high_zero)`.

| Species | Moisture curve | Interpretation |
|---|---|---|
| *C. cibarius* | `(0.18, 0.45, 0.72, 0.92)` | optimum at moderate-to-fairly-moist conditions |
| *C. tubaeformis* | `(0.25, 0.55, 0.82, 0.97)` | optimum shifted toward wetter conditions |

Neither curve treats `1.0` as optimal. Both decline to zero near saturation.

## Reused direct response curves

Recent-rain trigger:

| Species | Window weights | 1d curve, mm | 3d curve, mm |
|---|---|---|---|
| *C. cibarius* | `0.35 / 0.65` | `(0, 2, 10, 30)` | `(0, 4, 20, 55)` |
| *C. tubaeformis* | `0.30 / 0.70` | `(0, 1.5, 10, 30)` | `(0, 3, 22, 60)` |

Temperature remains the weighted mean of the existing per-window trapezoid
responses:

| Species | 3d | 7d | 14d |
|---|---|---|---|
| *C. cibarius* | weight `0.20`, curve `(4,10,18,25)` | `0.50`, `(4,9,17,24)` | `0.30`, `(3,8,16,23)` |
| *C. tubaeformis* | `0.15`, `(-1,5,14,21)` | `0.45`, `(-1,5,13.5,20)` | `0.40`, `(-2,4,13,19)` |

Season is unchanged: day-of-year curve `(175,215,275,315)` for *C. cibarius*
and `(225,255,305,340)` for *C. tubaeformis*.

## Availability, confidence, and exclusions

V2 accepts moisture only when:

- moisture status is `estimated_complete` or `estimated_optional_inputs_missing`;
- the estimate is non-null;
- validated static wetness, all six rain windows, all three temperature windows,
  and both RH windows were used.

Missing optional soil/slope is allowed under the moisture model's documented
neutral fallback. Its exact moisture confidence and completeness are copied into
`fruiting_v2_breakdown`. Missing/unvalidated central input returns
`insufficient_moisture`; no mock fallback is introduced.

Any production habitat exclusion returns `excluded_habitat` with both v2 scores
set to `None`. This includes open water and all validated NMD exclusions such as
artificial surfaces and agricultural land.

## Synthetic sensitivity scenarios

All scenarios use the same class-2, fine-mineral, 12-degree habitat. A/B use hot,
dry air to represent a dry period; C/D use suitable temperature and ordinary RH;
E combines extreme cumulative rain with 95% RH.

| Species | Scenario | Moisture | Moisture pref. | Recent trigger | Temp. | Season | Fruiting v2 |
|---|---|---:|---:|---:|---:|---:|---:|
| cibarius | A: dry month, no recent rain | 0.296000 | 0.429630 | 0.000000 | 0.000000 | 1.000000 | 0.393333 |
| cibarius | B: dry month, heavy last 24h | 0.592246 | 1.000000 | 0.860000 | 0.000000 | 1.000000 | 0.736000 |
| cibarius | C: wet month, no recent rain | 0.609513 | 1.000000 | 0.000000 | 1.000000 | 1.000000 | 0.900000 |
| cibarius | D: wet month, recent rain | 0.760693 | 0.796535 | 1.000000 | 1.000000 | 1.000000 | 0.908441 |
| cibarius | E: near saturation | 0.919964 | 0.000180 | 0.000000 | 1.000000 | 1.000000 | 0.450081 |
| tubaeformis | A: dry month, no recent rain | 0.296000 | 0.153333 | 0.000000 | 0.000000 | 0.933333 | 0.244667 |
| tubaeformis | B: dry month, heavy last 24h | 0.592246 | 1.000000 | 0.880000 | 0.000000 | 0.933333 | 0.756000 |
| tubaeformis | C: wet month, no recent rain | 0.609513 | 1.000000 | 0.000000 | 0.898718 | 0.933333 | 0.865718 |
| tubaeformis | D: wet month, recent rain | 0.760693 | 1.000000 | 1.000000 | 0.898718 | 0.933333 | 0.965718 |
| tubaeformis | E: near saturation | 0.919964 | 0.333573 | 0.000000 | 0.898718 | 0.933333 | 0.532505 |

The intended qualitative behavior holds: A is lowest, B improves without becoming
maximal, C remains good without yesterday's rain, D is highest or near-highest,
and E is penalized outside the moisture optimum.

## Real-point comparison

The reproducible full report contains every requested old and v2 component for
10 searchable points × 2 species:

```powershell
python scripts/compare_fruiting_v1_v2.py `
  --output reports/fruiting_v1_v2_real_points.json
```

Coverage:

| Point | MESAN grid | SLU | Soil | Slope |
|---|---|---:|---|---:|
| class1_coarse | 59.169136, 18.237348 | 1 | coarse_mineral | unavailable |
| class2_moraine | 59.169136, 18.237348 | 2 | moraine | unavailable |
| class3_fine | 59.169136, 18.237348 | 3 | fine_mineral | unavailable |
| class2_peat | 59.169136, 18.237348 | 2 | organic_peat | unavailable |
| class1_peat | 59.169136, 18.237348 | 1 | organic_peat | unavailable |
| class3_peat | 59.169136, 18.237348 | 3 | organic_peat | unavailable |
| flat_class2_fine | 59.418897, 18.129709 | 2 | fine_mineral | 0.662° |
| sloping_class1_bedrock | 59.418897, 18.129709 | 1 | bedrock_or_thin_soil | 16.273° |
| steep_class1_moraine | 59.418897, 18.129709 | 1 | moraine | 20.687° |
| sloping_class2_coarse | 59.418897, 18.129709 | 2 | coarse_mineral | 6.324° |

The old weather components are identical within a MESAN grid/species pair:

| Grid | Species | Precipitation | Temperature | RH | Season | Fruiting v1 |
|---|---|---:|---:|---:|---:|---:|
| 59.169136, 18.237348 | cibarius | 0.191525 | 1.000000 | 1.000000 | 1.000000 | 0.636186 |
| 59.169136, 18.237348 | tubaeformis | 0.105575 | 0.708939 | 1.000000 | 0.466667 | 0.436011 |
| 59.418897, 18.129709 | cibarius | 0.174567 | 0.974425 | 1.000000 | 1.000000 | 0.620883 |
| 59.418897, 18.129709 | tubaeformis | 0.093378 | 0.556528 | 0.998660 | 0.466667 | 0.387687 |

Summary comparison; the JSON report also contains habitat score, old final score,
moisture confidence/completeness, coordinates, land-cover labels, and exact grid
distances:

| Point | Species | Moisture / pref. | Recent | Fruiting v1 → v2 | Final delta |
|---|---|---:|---:|---:|---:|
| class1_coarse | cibarius | 0.191863 / 0.043937 | 0.008674 | 0.636186 → 0.470639 | -0.066219 |
| class1_coarse | tubaeformis | 0.191863 / 0.000000 | 0.011429 | 0.436011 → 0.241109 | -0.087706 |
| class2_moraine | cibarius | 0.489863 / 1.000000 | 0.008674 | 0.636186 → 0.900867 | +0.105873 |
| class2_moraine | tubaeformis | 0.489863 / 0.799543 | 0.011429 | 0.436011 → 0.640881 | +0.092191 |
| class3_fine | cibarius | 0.771863 / 0.740685 | 0.008674 | 0.636186 → 0.784176 | +0.059195 |
| class3_fine | tubaeformis | 0.771863 / 1.000000 | 0.011429 | 0.436011 → 0.741109 | +0.137294 |
| class2_peat | cibarius | 0.553863 / 1.000000 | 0.008674 | 0.636186 → 0.900867 | +0.105872 |
| class2_peat | tubaeformis | 0.553863 / 1.000000 | 0.011429 | 0.436011 → 0.741109 | +0.137294 |
| class1_peat | cibarius | 0.303863 / 0.458752 | 0.008674 | 0.636186 → 0.657306 | +0.008448 |
| class1_peat | tubaeformis | 0.303863 / 0.179543 | 0.011429 | 0.436011 → 0.330881 | -0.047309 |
| class3_peat | cibarius | 0.803863 / 0.580685 | 0.008674 | 0.636186 → 0.712176 | +0.030395 |
| class3_peat | tubaeformis | 0.803863 / 1.000000 | 0.011429 | 0.436011 → 0.741109 | +0.137294 |
| flat_class2_fine | cibarius | 0.537373 / 1.000000 | 0.078357 | 0.620883 → 0.901442 | +0.112224 |
| flat_class2_fine | tubaeformis | 0.537373 / 0.957910 | 0.106871 | 0.387687 → 0.696078 | +0.138776 |
| sloping_class1_bedrock | cibarius | 0.160152 / 0.000000 | 0.078357 | 0.620883 → 0.451442 | -0.067776 |
| sloping_class1_bedrock | tubaeformis | 0.160152 / 0.000000 | 0.106871 | 0.387687 → 0.217123 | -0.076754 |
| steep_class1_moraine | cibarius | 0.215324 / 0.130830 | 0.078357 | 0.620883 → 0.510315 | -0.044227 |
| steep_class1_moraine | tubaeformis | 0.215324 / 0.000000 | 0.106871 | 0.387687 → 0.217123 | -0.076754 |
| sloping_class2_coarse | cibarius | 0.446049 / 0.985367 | 0.078357 | 0.620883 → 0.894857 | +0.109590 |
| sloping_class2_coarse | tubaeformis | 0.446049 / 0.653497 | 0.106871 | 0.387687 → 0.543872 | +0.070283 |

## Observed behaviors requiring review

- Moderate moisture inside the flat optimum can dominate v2. At class-2 moraine
  and peat points, *C. cibarius* reaches about `0.901` despite a recent trigger
  below `0.009`. This follows the configured `0.45` moisture plus favorable
  temperature/season weights, but may be too permissive.
- *C. tubaeformis* rises by roughly `0.305` in fruiting score at several moist
  points because v1's low direct precipitation score is replaced by an optimal
  moisture preference. The curve and 0.50 weight need field calibration.
- Dry class-1 coarse/bedrock points move downward as intended.
- Near-saturation is penalized in synthetic tests, but no real point in this
  small sample reached the extreme falling edge for both species.
- Static wetness remains represented in both unchanged habitat score and the
  moisture-driven fruiting experiment's final score, as noted above.

## Limitations

- The sample has only two MESAN grid histories and one locally downloaded DEM
  tile; six points therefore have optional slope unavailable.
- Response curves and weights are uncalibrated and have not been compared with
  find/no-find labels.
- Moisture v1 limitations, including absence of snow/frozen-ground state and
  physical evapotranspiration, propagate into v2.
- The observed large positive deltas are evidence for review, not justification
  to promote v2.
- V2 must remain experimental until broader spatial, seasonal, and field-data
  validation has been completed.
