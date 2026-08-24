# Klassad SLU-markfuktighet som statisk fuktighetspotential

Denna integration behandlar kartan som platsens långsiktiga/statiska
hydrologiska fuktighetspotential. Den beskriver inte dagens markfuktighet och
delar varken modellfält eller scoringkomponent med framtida dynamisk fuktighet.

## Minnesbegränsad inspection

Kommando:

```powershell
python scripts/inspect_rasters.py src/data/misc_data/SLUMarkfuktighetKlassad.tif
```

| Egenskap | Resultat |
|---|---|
| Filstorlek | 8 440 050 454 byte (7,86 GiB) |
| CRS / EPSG | SWEREF 99 TM / EPSG:3006 |
| Upplösning | 2 × 2 m |
| Dimensioner | 325 000 × 770 000, ett band |
| Bounds | `(267499.9999999999, 6132499.999999999, 917499.9999999999, 7672499.999999999)` |
| Dtype | `uint8` |
| NoData | 255 |
| Min / max | 1 / 4 |
| Percentiler 1/5/25/50/75/95/99 | 1 / 1 / 1 / 1 / 2 / 4 / 4 |
| Block / komprimering | tiled 128 × 128 / LZW |
| Overviews | inga |
| Unit / band description | saknas / saknas |

Taggarna är `DataType=Generic`, `AREA_OR_POINT=Area`,
`PyramidResamplingType=NEAREST`, `COMPRESSION=LZW`, `INTERLEAVE=BAND` och
bandtaggen `RepresentationType=THEMATIC`. GeoTIFFens färgtabell har orange,
grön, blå och mörkblå färg för klass 1–4 samt transparent NoData=255. Den lokala
datakatalogen innehåller ingen sidecar-fil med klassnamn; färgtabellen och
`THEMATIC` bekräftar kategorisk struktur men inte klassernas betydelse.

Rastret innehåller 250 250 000 000 källpixlar. Statistiken kommer därför från
ett nearest-neighbour-grid på 1 539 × 649 = 998 811 pixlar, inte från en
heltäckande inläsning. Av dessa var 443 767 giltiga. Alla värden och frekvenser i
tabellen nedan är därför approximativa.

| Råklass | Samplat antal | Andel av samplade giltiga pixlar |
|---:|---:|---:|
| 1 | 262 070 | 59,0558 % |
| 2 | 88 498 | 19,9424 % |
| 3 | 43 835 | 9,8779 % |
| 4 | 49 364 | 11,1239 % |

Samplad NoData-andel var 55,5705 % och giltig andel 44,4295 %. JSON-rapporten
finns i `reports/raster_inspection/SLUMarkfuktighetKlassad.json`.

## Verifierad klassmappning

Skogsstyrelsens officiella produktbeskrivning för Markfuktighetskarta SLU anger:

| Råklass | Officiell etikett |
|---:|---|
| 1 | torr-frisk |
| 2 | frisk-fuktig |
| 3 | fuktig-blöt |
| 4 | öppet vatten |

Källa: [Markfuktighetskarta SLU – produktbeskrivning](https://www.skogsstyrelsen.se/globalassets/sjalvservice/karttjanster/geodatatjanster/produktbeskrivningar/markfuktighetskarta-slu---produktbeskrivning.pdf).
SLU beskriver kartan som förväntad fuktighet över en stor del av året, alltså
inte momentan väderstyrd fuktighet: [Om SLU-markfuktighetskartor](https://www.slu.se/om-slu/organisation/institutioner/skogens-ekologi-skotsel/forskning/teman/digital-landskap/markfuktighetskartor/om-slu-markfuktighetskartor/).

Adapterns säkra standardläge har ingen mapping och ger då
`raw_class=<värde>`, `static_wetness_class=None` och status
`raw_class_preserved_semantics_unvalidated`. `slu_classified()` väljer den
ovanstående källbelagda mappningen explicit och sätter status
`validated_official_class_mapping`.

## Verkliga punktuppslag

Alla punkter transformerades självständigt från WGS84 till rastrets CRS och
lästes som ett enda 1 × 1-fönster.

| Punkt | WGS84 lat, lon | Råklass / etikett | Pixel row, col | NoData |
|---|---|---|---|---|
| Befintlig testpunkt | 59.412, 18.132 | 4 / öppet vatten | 541193, 205121 | false |
| Jämtland | 63.100, 14.300 | 1 / torr-frisk | 337788, 98582 | false |
| Småland | 57.250, 14.600 | 2 / frisk-fuktig | 663606, 104182 | false |
| Store Mosse | 57.270, 13.920 | 3 / fuktig-blöt | 662270, 83684 | false |
| Muddus | 66.950, 20.150 | 3 / fuktig-blöt | 118694, 228651 | false |

Platsnamnen är endast geografiska provetiketter; tabellen gör inget oberoende
anspråk på vilken klass punkterna borde ha. Testpunkten i Stockholm ger öppet
vatten, vilket är en rimlig signal att granska punktens lokala geometri och
skillnader mellan rasterfamiljernas upplösning/grid innan biologiska slutsatser.

## Modell och scoring

Nya statiska modellfält:

- `static_wetness_class: int | None`
- `static_wetness_label: str | None`

Framtida dynamisk fuktighet heter separat
`estimated_current_soil_moisture_index` i `DynamicWeatherFeatures`.

När klassen är ovaliderad påverkas habitat-, fruiting-, final score och
confidence inte alls. När den officiella mappningen är vald aktiveras den separata
habitatkomponenten `static_wetness`. Klassbetydelsen är validerad, men följande
artspecifika preferensvikter är uttryckligen biologiskt preliminära:

| Art | klass 1 | klass 2 | klass 3 | klass 4 |
|---|---:|---:|---:|---:|
| Cantharellus cibarius | 0,55 | 1,00 | 0,75 | 0,00 |
| Craterellus tubaeformis | 0,35 | 0,90 | 1,00 | 0,00 |

Vid `59.412, 18.132` (klass 4) blev effekten:

| Art | Habitat före → efter | Final före → efter | Confidence |
|---|---|---|---:|
| Cantharellus cibarius | 0,927532 → 0,788402 | 0,950919 → 0,867441 | 0,410 |
| Craterellus tubaeformis | 0,918040 → 0,780334 | 0,875699 → 0,799961 | 0,405 |

`confidence` är oförändrat eftersom nuvarande hybridpolicy fortfarande använder
den konservativa mockkvaliteten på habitatnivå.
