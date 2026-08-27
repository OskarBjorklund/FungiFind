# NMD2023 basskikt v2.1 som marktäckedata

## Lokala och officiella källor

Den angivna sökvägen `src/data/landcover/NMD2023bas_v2_1.tif` fanns inte i den
lokala arbetskopian. Den oförändrade rasterleveransen hittades i stället här:

- `src/data/base_layer/NMD2023bas_v2_1.tif`
- `src/data/base_layer/NMD2023bas_v2_1.tif.vat.dbf`
- `src/data/base_layer/NMD2023bas_v2_1.tif.ovr`
- `src/data/base_layer/NMD2023bas_v2_1.tfw`

CLI-adaptern söker först på den avsedda `landcover`-sökvägen och använder därefter
den befintliga `base_layer`-sökvägen. Ingen rasterfil flyttas eller ändras.

Klasskoderna verifierades mot både den levererade `.vat.dbf`-tabellen och
Naturvårdsverkets officiella produktbeskrivning för NMD2023 basskikt v2.1,
Bilaga 1 (nomenklatur) och Bilaga 2 (klassdefinitioner):

<https://geodata.naturvardsverket.se/nedladdning/marktacke/NMD2023/Basskikt_v2_x/NMD2023_Produktbeskrivning_Basskikt_NMD2023_v2_1.pdf>

Adaptern läser och kontrollerar den lokala VAT-tabellen när den finns. En avvikelse
mellan kodens mapping och den levererade tabellen stoppar initieringen.

## Rasterinspektion

Inspektionen gjordes med `scripts/inspect_rasters.py`. För statistik och klassfrekvenser
användes en närmaste-granne-sampling på 998 448 pixlar; hela 10,11 GiB-rastret lästes
inte in i RAM.

| Egenskap | Resultat |
|---|---|
| Filstorlek | 10 852 673 468 byte (10,11 GiB) |
| Format | GeoTIFF, PackBits, tiled |
| CRS / EPSG | SWEREF 99 TM / EPSG:3006 |
| Upplösning | 10 x 10 meter |
| Dimensioner | 71 273 x 157 991, ett band |
| Bounds | left 208450, bottom 6091140, right 921180, top 7671050 |
| Dtype | `uint16` |
| NoData | inte deklarerat (`None`) |
| Min / max | 0 / 4233 |
| Percentiler p1/p5/p25/p50/p75/p95/p99 | 0 / 0 / 0 / 0 / 111 / 122 / 4222 |
| Bandbeskrivning | saknas |
| Enhet | saknas; bandet är kategoriskt |
| Blockstorlek | 128 x 128 |
| Overviews | 2, 4, 8, 16, 32, 64, 128, 255, 509 |

Dataset-tags:

- `DataType=Generic`
- `AREA_OR_POINT=Area`
- `IMAGE_STRUCTURE: COMPRESSION=PACKBITS, INTERLEAVE=BAND`
- `Esri: PyramidResamplingType=NEAREST`

Band-tags:

- `RepresentationType=THEMATIC`
- `STATISTICS_MINIMUM=0`
- `STATISTICS_MAXIMUM=4233`
- `STATISTICS_MEAN=107.0323641973`
- `STATISTICS_STDDEV=517.12713790474`

Kod `0` har tom etikett i VAT och är inte en officiell NMD-klass. Rasterfilen deklarerar
den inte som teknisk NoData. Adaptern bevarar därför råvärdet men ger ingen tolkad
klass och status `unknown_class_not_in_validated_mapping`; den skapar ingen exclusion.

## Observerade klassvärden och ungefärlig frekvens

Följande 53 värden observerades. Procentsatserna är inspectorns approximativa andel
av alla tekniskt giltiga pixlar, inklusive bakgrundsvärdet 0.

| Kod | % | Kod | % | Kod | % |
|---:|---:|---:|---:|---:|---:|
| 0 | 53,149 | 3 | 2,256 | 23 | 0,102 |
| 43 | 0,143 | 51 | 0,095 | 52 | 0,094 |
| 53 | 0,709 | 54 | 0,018 | 61 | 2,924 |
| 62 | 13,834 | 111 | 6,038 | 112 | 3,382 |
| 113 | 2,790 | 114 | 2,394 | 115 | 1,688 |
| 116 | 0,436 | 117 | 0,207 | 118 | 3,316 |
| 121 | 1,135 | 122 | 0,238 | 123 | 0,317 |
| 124 | 0,417 | 125 | 0,396 | 126 | 0,001 |
| 127 | 0,005 | 128 | 0,094 | 200 | 0,436 |
| 211 | 0,418 | 212 | 0,209 | 213 | 0,464 |
| 214 | 0,030 | 215 | 0,039 | 216 | 0,125 |
| 217 | 0,254 | 218 | 0,001 | 221 | 0,076 |
| 222 | 0,012 | 223 | 0,043 | 224 | 0,021 |
| 225 | 0,039 | 226 | 0,008 | 227 | 0,025 |
| 411 | 0,093 | 4211 | 0,038 | 4212 | 0,102 |
| 4213 | 0,078 | 4221 | 0,121 | 4222 | 0,280 |
| 4223 | 0,187 | 4231 | 0,061 | 4232 | 0,291 |
| 4233 | 0,372 |  |  |  |  |

## Officiell kodmapping

| Kod | Officiell svensk etikett | Eligibility |
|---:|---|---|
| 3 | Åkermark | excluded: `agricultural_land` |
| 23 | Låg fjällskog på våtmark | searchable |
| 43 | Låg fjällskog på fastmark | searchable |
| 51 | Byggnad | excluded: `built_or_artificial_land` |
| 52 | Anlagd mark, ej byggnad eller väg/järnväg | excluded: `built_or_artificial_land` |
| 53 | Väg eller järnväg | excluded: `built_or_artificial_land` |
| 54 | Torvtäkt | excluded: `built_or_artificial_land` |
| 61 | Inlandsvatten | excluded: `open_water` |
| 62 | Hav | excluded: `open_water` |
| 111 | Tallskog på fastmark | searchable |
| 112 | Granskog på fastmark | searchable |
| 113 | Barrblandskog på fastmark | searchable |
| 114 | Lövblandad barrskog på fastmark | searchable |
| 115 | Triviallövskog på fastmark | searchable |
| 116 | Ädellövskog på fastmark | searchable |
| 117 | Triviallövskog med ädellövinslag på fastmark | searchable |
| 118 | Temporärt ej skog på fastmark | searchable |
| 121 | Tallskog på våtmark | searchable |
| 122 | Granskog på våtmark | searchable |
| 123 | Barrblandskog på våtmark | searchable |
| 124 | Lövblandad barrskog på våtmark | searchable |
| 125 | Triviallövskog på våtmark | searchable |
| 126 | Ädellövskog på våtmark | searchable |
| 127 | Triviallövskog med ädellövinslag på våtmark | searchable |
| 128 | Temporärt ej skog på våtmark | searchable |
| 200 | Öppen våtmark (underindelning saknas) | searchable |
| 211 | Buskmyr | searchable |
| 212 | Ristuvemyr | searchable |
| 213 | Fastmattemyr, mager | searchable |
| 214 | Fastmattemyr, frodig | searchable |
| 215 | Sumpkärr | searchable |
| 216 | Mjukmattemyr | searchable |
| 217 | Lösbottenmyr | searchable |
| 218 | Övrig öppen myr | searchable |
| 221 | Våtmark med buskar | searchable |
| 222 | Risdominerad våtmark | searchable |
| 223 | Gräsdominerad våtmark, mager | searchable |
| 224 | Gräsdominerad våtmark, frodvuxen | searchable |
| 225 | Gräsdominerad våtmark, högvuxen | searchable |
| 226 | Mossdominerad våtmark | searchable |
| 227 | Våtmark utan växttäcke | searchable |
| 228 | Övrig öppen våtmark | searchable |
| 411 | Öppen fastmark utan vegetation (ej glaciär eller varaktigt snöfält) | searchable |
| 412 | Glaciär | excluded: `permanent_ice_or_snow`; officiell men ej observerad i lokal VAT |
| 413 | Varaktigt snöfält | excluded: `permanent_ice_or_snow`; officiell men ej observerad i lokal VAT |
| 4211 | Torr buskdominerad mark | searchable |
| 4212 | Frisk buskdominerad mark | searchable |
| 4213 | Frisk-fuktig buskdominerad mark | searchable |
| 4221 | Torr risdominerad mark | searchable |
| 4222 | Frisk risdominerad mark | searchable |
| 4223 | Frisk-fuktig risdominerad mark | searchable |
| 4231 | Torr gräsdominerad mark | searchable |
| 4232 | Frisk gräsdominerad mark | searchable |
| 4233 | Frisk-fuktig gräsdominerad mark | searchable |

Våtmark är avsiktligt inte en exclusion. Skogsmark och övrig naturmark används inte
som nya kontinuerliga preferenskomponenter i detta steg.

## Verkliga kontroller

Poängen nedan gäller `Cantharellus cibarius`, datum 2026-08-23, med befintliga mockade
övriga habitat- och väderfeatures. Syftet är att visa att NMD bara påverkar eligibility.

| Typ | WGS84 lat/lon | Råklass och etikett | Status | Slutscore |
|---|---|---|---|---:|
| Skog | 59.1699762, 18.2500590 | 113 Barrblandskog på fastmark | eligible | 0,965161 |
| Tätort/anlagd mark | 59.3293166, 18.0686566 | 52 Anlagd mark, ej byggnad eller väg/järnväg | excluded | inget index |
| Jordbruksmark | 59.8527270, 17.6031023 | 3 Åkermark | excluded | inget index |
| Vatten | 59.4120124, 18.1320245 | 62 Hav | excluded | inget index |
| Våtmark | 57.2800335, 13.9199590 | 216 Mjukmattemyr | eligible | 0,977529 |

För de två giltiga punkterna är score identisk med tidigare pipeline för samma punkt.
För exkluderade punkter är `habitat_score`, `fruiting_score` och `final_score` `None`,
`eligibility_status` är `excluded`, och `score_type` är
`excluded_habitat_no_suitability_index_v0`.

Om både SLU-markfuktighetsrastret och NMD anger exclusion behålls båda som separata
`HabitatExclusion`-poster med källfeature, källnamn, källsökväg, råvärde och semantisk
status. Endast provenance med en status som börjar med `validated` får exkludera.
