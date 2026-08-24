# Gran_andel.tif: första rådatakontrakt

Den här sidan dokumenterar endast vad den lokala GeoTIFF-filen och den genererade
inspection-rapporten visar. Den är inte dokumentation av datasetets officiella
semantik.

## Observerad rastermetadata

| Fält | Observerat värde |
|---|---|
| Fil | `src/data/kind/Gran_andel.tif` |
| Storlek | 3 496 001 500 byte (3,26 GiB) |
| CRS | `EPSG:25833` |
| Upplösning | 12,5 × 12,5 CRS-enheter |
| Dimensioner | 52 600 × 123 200, ett band |
| Bounds | left 265000, bottom 6132500, right 922500, top 7672500 |
| Dtype | `int16` |
| NoData | `-1` |
| Bandbeskrivning | saknas |
| Enhet | saknas |
| Komprimering | LZW |
| Block | strippar om 52 600 × 20 pixlar |
| Overviews | 2, 4, 8, 16, 32, 64 |

Dataset-taggarna anger `AREA_OR_POINT=Area`, `COMPRESSION=LZW` och
`INTERLEAVE=BAND`. De anger inte variabelns semantik eller enhet.

Den minnesbegränsade inspectionen använde ett representativt grid med 999 090
pixlar. Resultaten är därför approximativa: min 0, max 100, medel 20,0720,
standardavvikelse 29,4578 och percentilerna p1=0, p5=0, p25=0, p50=0, p75=37,
p95=87 och p99=96.

## Semantisk status

Filnamnet `Gran_andel` och heltalsintervallet 0–100 ger stöd för hypotesen att ett
råvärde är någon form av granandel på en 0–100-skala. Det finns däremot ingen enhet,
bandbeskrivning eller annan lokal metadata som bekräftar att värdet är procent.

Standardläget i `ForestRasterDataSource` bevarar därför råvärdet men lämnar
`spruce_fraction=None`. Ett separat, uttryckligen valt preliminärt läge dividerar
0–100-värdet med 100 och märker resultatet som semantiskt ovaliderat. Råvärde,
pixelindex, CRS och källfil finns alltid kvar i feature-provenance.

## Verifierat punktuppslag

Uppslaget för WGS84 `59.412, 18.132` gav följande faktiska resultat:

| Fält | Resultat |
|---|---|
| Raster-CRS | `EPSG:25833` |
| Projicerad koordinat | x=677743,673; y=6590113,560 |
| Pixel | row=86590, col=33019 |
| Råvärde | `7` |
| NoData | false |

I säkert standardläge är `spruce_fraction=None` och råvärdet 7 bevaras. I det
uttryckligen valda preliminära 0–100-läget blir `spruce_fraction=0.07`; provenance
anger samtidigt att omräkningen inte är semantiskt validerad. Filstorlek och
skrivtid var oförändrade efter läsningen.
