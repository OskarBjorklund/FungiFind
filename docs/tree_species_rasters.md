# Trädslagsprofil: rasterinspektion och kontrakt

Alla statistikvärden nedan är approximativa och kommer från den minnesbegränsade
inspectionens representativa grid om 999 090 pixlar per raster.

## Gemensam struktur

Alla fyra filer har `EPSG:25833`, 12,5 × 12,5 upplösning, dimensionerna
52 600 × 123 200, bounds `(265000, 6132500, 922500, 7672500)`, ett `int16`-band,
NoData `-1`, LZW-komprimering, strippar om 52 600 × 20 och overviews
2/4/8/16/32/64.

Samtliga har taggarna `AREA_OR_POINT=Area`, `COMPRESSION=LZW` och
`INTERLEAVE=BAND`. Ingen fil har bandbeskrivning eller enhet som bekräftar den
semantiska skalan.

| Raster | Min | Max | P1 | P5 | P25 | P50 | P75 | P95 | P99 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Gran_andel | 0 | 100 | 0 | 0 | 0 | 0 | 37 | 87 | 96 |
| Tall_andel | 0 | 100 | 0 | 0 | 0 | 0 | 28 | 82 | 97 |
| Bjork_andel | 0 | 100 | 0 | 0 | 0 | 0 | 7 | 36 | 63 |
| OvrLov_andel | 0 | 97 | 0 | 0 | 0 | 0 | 0 | 10 | 33 |

Filnamnen och de observerade heltalsintervallen stödjer en preliminär hypotes om
andelar på skalan 0–100, men detta är inte officiellt bekräftat av den lokala
metadatan. Profilkällan bevarar därför alltid råvärden och kräver ett explicit
preliminärt tolkningsläge för division med 100.

## Verifierat uppslag och modelleffekt

För WGS84 `59.412, 18.132` använder alla fyra raster `EPSG:25833` och träffar
pixel `row=86590, col=33019`.

| Feature | Raster | Råvärde | Preliminär fraktion |
|---|---|---:|---:|
| `spruce_fraction` | Gran_andel | 7 | 0,07 |
| `pine_fraction` | Tall_andel | 74 | 0,74 |
| `birch_fraction` | Bjork_andel | 2 | 0,02 |
| `other_deciduous_fraction` | OvrLov_andel | 17 | 0,17 |

Den oförändrade summan är 1,00 och klassas `near_one`; inga features är missing
eller NoData. Detta är en sanity check, inte bevis för semantiken och inte en
automatisk normalisering.

Med övriga habitatfeatures och väder fortsatt mockade ändrades kantarellresultatet
från gran-only `final_score=0.955717` till fyralagersprofil
`final_score=0.950919`, en differens på -0.004798. Trädslagsfaktorn ändrades från
0.762615 till 0.729300. Confidence ökade från 0.387320 till 0.410000 eftersom de
fyra konfigurerade trädslagsandelarna är kompletta; confidence är fortfarande en
datakompletteringsindikator, inte statistisk säkerhet.
