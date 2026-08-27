# SMHI MESAN historical backfill

## Official source

FungiFind uses SMHI's open historical **MESAN (GRIDPP)** archive:

- dataset: Meteorologisk analysmodell MESAN (GRIDPP), historical analyses
- Atom feed: `https://opendata-download-grid-archive.smhi.se/feed/9`
- format: GRIB2
- cadence: one analysis file per UTC hour, archived through the preceding day
- published history: February 2024 onward
- horizontal grid: Lambert conformal, 2,500 x 2,500 metres, 949 x 1,069 cells
- domain: Scandinavia, parts of northern continental Europe and the Baltic states

The requested hour is encoded in both the Atom entry and filename:

```text
MESAN_YYYYMMDDHHMM+000H00M
```

The importer reads the month-level Atom feeds to discover which dates officially
exist. It then validates the corresponding hourly file with HTTP `HEAD`, including
`Content-Length` and `Last-Modified`, before sampling it.

Observed source sizes in the pilot period vary with UTC hour and field content:

| UTC analysis | Bytes | MiB |
|---|---:|---:|
| 2026-08-26 00Z | 73,047,288 | 69.66 |
| 2026-08-26 06Z | 85,221,860 | 81.27 |
| 2026-08-26 12Z | 70,003,845 | 66.76 |
| 2026-08-26 18Z | 76,091,319 | 72.57 |

Downloading all complete files for a 30-day point backfill would therefore be on
the order of 50 GiB. The importer does not do that.

## Parameters and compatibility

SMHI's official MESAN code table defines the raw units used here. The importer
also checks GRIB discipline/category/parameter, level, valid time, packing, grid
section and scanning mode before accepting a value.

| Stored field | Historical GRIB field | Raw unit | Conversion |
|---|---|---|---|
| `air_temperature_c` | TMP, 2 m | K | subtract 273.15 |
| `precipitation_1h_mm` | one-hour precipitation, surface | mm | none |
| `relative_humidity_percent` | RH, 2 m | fraction | multiply by 100 |
| `wind_speed_m_s` | UGRD/VGRD, 10 m | m/s | `hypot(u, v)` |

The GDAL generic label for the precipitation message calls it a rate, but the
SMHI MESAN parameter table identifies the product value as **one-hour
precipitation in mm**. Direct overlap with MESAN2G v3 also matches when the packed
value is treated as mm, without a `* 3600` conversion.

Both sources use the analysis valid time in UTC as the stored hourly timestamp.
For one-hour precipitation the GRIB reference time is the preceding hour and the
valid/end time is the filename hour.

For the pilot point, the historical GRIB and live MESAN2G v3 snap to the same cell
centre:

```text
requested: 59.412, 18.132
returned:  59.418896659, 18.129708938
stored:    59.418897, 18.129709
```

The compatibility is verified, not assumed. Every historical file preserves a
grid identity computed as SHA-256 of GRIB2 section 3. A changed grid is mapped
independently and stored under its own snapped coordinate/grid identity.

## Streaming and cache

The GRIB2 fields use simple packing. The importer:

1. discovers and validates one message layout per UTC hour represented, because
   SMHI varies the fields present through the day;
2. downloads one approximately 3 MiB temperature message as a reusable rasterio
   grid template;
3. maps WGS84 to the nearest grid-cell centre without interpolation;
4. fetches only 4 KiB metadata/value blocks for TMP, U, V, RH and one-hour
   precipitation;
5. stores those source byte ranges under
   `src/data/weather/mesan_archive_cache/`.

Cache writes use `.part` followed by atomic rename. Status, `Content-Range`, exact
byte count and complete template length are validated. Corrupt downloads are
removed and the SQLite archive is not opened for writing until discovery and all
GRIB parsing have succeeded.

## SQLite provenance and precedence

Schema version 2 adds:

- `source_product`
- `source_file`
- `grid_identity`
- `sampling_method`
- requested latitude/longitude
- `source_units_json`
- `source_priority`

Precedence is explicit:

```text
live MESAN2G v3 (priority 100)
    > historical MESAN GRIDPP feed 9 (priority 50)
```

Historical rows fill gaps and never overwrite live rows. A later live ingestion
may replace an existing lower-priority historical row for the same grid cell and
UTC hour. Differing live/historical values are reported before insertion.

## Commands

```powershell
python scripts/backfill_mesan_history.py `
  --latitude 59.412 `
  --longitude 18.132 `
  --days 30

python scripts/sample_weather_history.py `
  --latitude 59.412 `
  --longitude 18.132
```

`days_since_significant_rain` and `dry_spell_length` intentionally remain
disabled until an explicit threshold is supplied, for example:

```powershell
python scripts/sample_weather_history.py `
  --latitude 59.412 `
  --longitude 18.132 `
  --significant-rain-threshold-mm 1.0
```

The importer does not select a biological threshold and does not alter scoring.
