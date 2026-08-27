# Lantmäteriet DTM: elevation, slope och aspect

Integrationen läser en lokalt nedladdad `dtm-cog`-tile via STAC-manifestet och
producerar tre statiska terrängfeatures. De är tillgängliga för
`read → derive → validate`, men är inte kopplade till scoringservicen.

## Pilotdownload

Kommando:

```powershell
python scripts/download_elevation.py --min-lon 18.131 --min-lat 59.411 --max-lon 18.133 --max-lat 59.413
```

STAC item `659_67`, asset `data`, gav filen `m659_67.tif` i
`src/data/elevation/`. Filen fanns komplett när kommandot kördes och validerades
utan duplicerad download.

| Kontroll | Resultat |
|---|---|
| STAC/manifeststorlek | 272 835 442 byte |
| Lokal storlek | 272 835 442 byte |
| STAC checksum | `1220e7c33c09104322b07013b8140b1091b8069020f504a687fab6c9a85281d43fd7` |
| Lokal SHA-256 | `e7c33c09104322b07013b8140b1091b8069020f504a687fab6c9a85281d43fd7` |
| Checksum match | ja; `1220` är multihashprefix för SHA-256/32 byte |
| Kvarvarande `.part` | 0 |

## Rasterinspection

Inspectorn kördes med:

```powershell
python scripts/inspect_rasters.py src/data/elevation/m659_67.tif
```

| Egenskap | Resultat |
|---|---|
| CRS / EPSG | SWEREF99 TM + RH2000 height / EPSG:5845 |
| Upplösning | 1 × 1 m |
| Dimensioner | 10 000 × 10 000, ett band |
| Bounds | `(670000, 6590000, 680000, 6600000)` |
| Dtype | `float32` |
| NoData | `-9999` |
| Approximerat min / max | `-0,2230789 / 96,2607269 m` |
| Unit | `metre` |
| Band description | saknas |
| Block | tiled 512 × 512 |
| Komprimering | DEFLATE, predictor 3 |
| Layout | COG |
| Overviews | 2, 4, 8, 16, 32 |

Default-taggarna är `OVERVIEW_RESAMPLING=BILINEAR` och `AREA_OR_POINT=Area`.
`IMAGE_STRUCTURE` innehåller `LAYOUT=COG`, `COMPRESSION=DEFLATE`,
`INTERLEAVE=BAND` och `PREDICTOR=3`. Bandet saknar egna taggar.

Statistiken är ett nearest-neighbour-sample om 1 000 × 1 000 pixlar; hela
100-miljonerspixelrastret lästes inte till RAM. Full rapport finns i
`reports/raster_inspection/m659_67.json`.

## Compound CRS EPSG:5845

Rasterio rapporterar `EPSG:5845`. Pyproj identifierar detta som ett compound CRS
med följande delar:

- `SWEREF99 TM`, projicerat horisontellt CRS `EPSG:3006`.
- `RH2000 height`, vertikalt CRS `EPSG:5613`.

Tvådimensionell pixeluppslagning ska inte behandla den vertikala axeln som en
lat/lon-koordinat. Den gemensamma rasterinfrastrukturen extraherar därför den
enda projicerade/geografiska del-CRS:en ur ett compound CRS och bygger
`Transformer.from_crs("EPSG:4326", horizontal_crs, always_xy=True)`.

Compound `EPSG:5845` bevaras i provenance. Den horisontella transformen använder
explicit `EPSG:3006`; den vertikala delen dokumenterar att höjdvärdet är i
RH2000. För pilotpunkten ger både direkt compound-transform och den explicita
horisontella transformen `(677743.6733, 6590113.5602)`.

## Horn 3×3

Slope och aspect beräknas med Horns viktade 3×3-finitdifferens. För
höjdgrannskapet

```text
z1 z2 z3
z4 z5 z6
z7 z8 z9
```

beräknas höjdgradienten österut och norrut som:

```text
dz_east  = ((z3 + 2*z6 + z9) - (z1 + 2*z4 + z7)) / (8 * pixel_width)
dz_north = ((z1 + 2*z2 + z3) - (z7 + 2*z8 + z9)) / (8 * pixel_height)
```

Slope är `atan(hypot(dz_east, dz_north))` uttryckt i grader. Aspect är
nedförsriktningen, med konventionen:

```text
0° = north
90° = east
180° = south
270° = west
```

Aspect normaliseras till `[0, 360)`. På helt plan yta är slope 0° och aspect
`None`.

## NoData och tilekant

- Central NoData: elevation, slope och aspect blir `None`.
- Giltigt centrum men NoData/icke-finit granne: elevation bevaras; slope/aspect
  blir `None`.
- Centrum på första/sista raden eller kolumnen: elevation bevaras; slope/aspect
  blir `None` med `insufficient_neighborhood_at_tile_edge`.
- Utanför alla manifesttiles: `ElevationTileNotFoundError`.
- En framtida mosaik kan komplettera 3×3-grannskap över tilegräns; nuvarande
  konservativa version gissar inte saknade grannvärden.

## Manifestbaserat tile-index

`ElevationTileIndex` läser alla lokala GeoTIFF-dataassets i manifestet. Lookup:

1. filtrerar på itemets WGS84-bbox,
2. transformerar punkten till varje kandidatraster horisontella CRS,
3. verifierar faktiskt row/col mot rasterdimensionerna,
4. föredrar en kandidat som rymmer ett komplett 3×3-grannskap.

Lokala filvägar valideras så att de inte kan lämna manifestkatalogen. Samma index
fungerar med flera framtida 10×10 km-tiles utan hårdkodade item-id:n.

## Pilotpunkt

För WGS84 `59.412, 18.132`:

| Egenskap | Resultat |
|---|---|
| DEM item / fil | `659_67` / `m659_67.tif` |
| Pixel | row 9886, col 7743 |
| Rå/tolkad elevation | `0,1000000015 m` RH2000 |
| Slope | `0,0°` |
| Aspect | `None` (`flat_surface_aspect_undefined`) |
| Metod | Horn 3×3 |

`TerrainDemReader` returnerar ett `FeatureSnapshot[StaticHabitatFeatures]`, men
har avsiktligt ingen `get_features`-adapter till `MushroomScoringService` ännu.
Tidigare habitat- och final-score är därmed oförändrade.
