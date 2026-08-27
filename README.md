# FungiFind

FungiFind är en första vertikal prototyp för en svensk kantarell- och
trattkantarellindikator. Den tar en WGS84-koordinat, ett datum och en art och
returnerar ett **heuristiskt suitability/index score** mellan 0 och 1. Resultatet är
inte en statistisk sannolikhet och säger inte att svamp faktiskt finns på platsen.

## Status och avgränsning

Version 0 kan kombinera riktiga trädslags-, skogsstruktur-, klassade statiska
markfuktighets- och marktäckelager med syntetiska fallbackfeatures. Ett lokalt
30-dygns MESAN-arkiv kan nu leverera coverage-verifierade nederbörds-, temperatur-
och luftfuktighetsaggregat via `MesanWeatherHistoryDataSource`. Det enkla
`fungifind`-kommandot använder fortfarande den fristående mockkonfigurationen;
`scripts/sample_mesan_model.py` kör den lokala MESAN-integrationen.

Alla biologiska preferensintervall och vikter är preliminära antaganden för att
testa programflödet. De är inte forskningsvaliderade, tränade eller kalibrerade mot
fynddata. `confidence` är endast en indikator för datakomplettering och grov
källkvalitet, inte ett statistiskt konfidensmått.

## Arkitektur

```text
src/fungifind/
├── models.py              # typade domänmodeller och enheter
├── config.py              # separata, preliminära artparametrar
├── scoring.py             # utbytbar regelbaserad scoring engine
├── service.py             # orkestrerar hela flödet
├── geo.py                 # valfri CRS-transformering utanför domänlogiken
└── data_sources/
    ├── base.py            # Protocol-interface för habitat och väder
    └── mock.py            # syntetiska datapunkter
tests/
├── test_models.py
└── test_scoring.py
```

Flödet är:

```text
WGS84-plats + datum + art
        ↓
HabitatDataSource + WeatherDataSource
        ↓
StaticHabitatFeatures + DynamicWeatherFeatures
        ↓
RuleBasedScoringEngine + artspecifik SpeciesConfig
        ↓
ModelResult
```

Datakällorna är `Protocol`-interface. En framtida Rasterio/GeoPandas-adapter eller
SMHI-klient behöver bara returnera samma `FeatureSnapshot`-modeller; service- och
scoringlagren behöver inte känna till filformat, API:er eller CRS. API-input är
alltid WGS84. `geo.project_location()` kan transformera till det CRS som en viss
rasteradapter kräver, utan att SWEREF 99 TM hårdkodas i domänlogiken.

`ScoringEngine` är också ett interface. En tränad scikit-learn-, XGBoost- eller
LightGBM-modell kan senare implementera samma gräns och återanvända datalagret och
ett framtida FastAPI-kontrakt.

## Beräkning i version 0

Varje underkomponent beräknas med transparenta trapetsformade preferenskurvor och
vikter från `config.py`. Saknade värden anges med `None`, rapporteras i
`missing_features` och utesluts ur score-medelvärdet, samtidigt som de sänker
`confidence`.

```text
habitat_score = viktat medelvärde av
  forest, tree_species, soil_moisture, terrain, soil, static_wetness

fruiting_score = viktat medelvärde av
  recent_rain, medium_term_rain, background_rain,
  temperature, relative_humidity, season
```

För kantarell är slutformeln:

```text
final_score = 0.60 × habitat_score + 0.40 × fruiting_score
```

För trattkantarell är den:

```text
final_score = 0.55 × habitat_score + 0.45 × fruiting_score
```

Om en hel domän saknas normaliseras vikten över den domän som finns, medan
`confidence` fortfarande straffas. Alla del- och slutresultat klipps naturligt till
0–1 genom preferenskurvorna och viktade medelvärden.

## Installation och körning

Basprototypen har inga externa runtime-beroenden. Skapa gärna en virtuell miljö och
installera paketet inklusive testverktyg:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pytest
```

Kör Python-API:t:

```python
from fungifind import get_mushroom_score

result = get_mushroom_score(
    latitude=59.412,
    longitude=18.132,
    date="2026-08-23",
    species="cantharellus_cibarius",
)

print(result.to_dict())
```

Eller CLI:t:

```powershell
fungifind --latitude 59.412 --longitude 18.132 --date 2026-08-23 `
  --species cantharellus_cibarius
```

Installera framtida geoberoenden först när en riktig adapter utvecklas:

```powershell
python -m pip install -e ".[geo,ml,dev]"
```

GDAL installeras normalt transitivt eller via plattformens geospatiala miljö; det
behövs inte för mockflödet.

## Inspektera råa GeoTIFF-filer

`scripts/inspect_rasters.py` beskriver råa raster utan att tolka värdena eller
ändra GeoTIFF-filerna. Det kan ta emot en enskild `.tif`/`.tiff` eller alla sådana
filer direkt i en katalog. Installera de fokuserade rasterberoendena:

```powershell
python -m pip install -e ".[raster,dev]"
```

Exempel:

```powershell
python scripts/inspect_rasters.py data/Gran_andel.tif
python scripts/inspect_rasters.py data/
```

I repositoryts nuvarande datastruktur kan samma verktyg köras så här:

```powershell
python scripts/inspect_rasters.py src/data/kind/Gran_andel.tif
python scripts/inspect_rasters.py src/data/kind/
```

Varje raster ger två filer:

```text
reports/raster_inspection/<filnamn>.json
reports/raster_inspection/<filnamn>_preview.png
```

JSON-rapporten innehåller CRS, dimensioner, upplösning, bounds, transform,
datatyper, NoData, bandbeskrivningar, enheter, metadata/taggar, komprimering,
blockstorlek, overviews och statistik per band. Previewn visar band 1 i neutral
gråskala. Dess linjära min/max-skalning gäller endast PNG-bilden och påverkar inte
källrastret. För heltalsraster rapporteras även en minnesbegränsad
klassfördelning när antalet observerade unika värden inte överstiger 256.

Raster med högst 10 miljoner pixlar totalt helskannas i fönster om högst
1024 × 1024 pixlar. Större raster läses som ett representativt nearest-neighbour-
grid med högst en miljon pixlar per band. Rapportens `statistics.mode`,
`are_approximate` och `approximate_fields` anger uttryckligen när värden är
approximativa. Percentilunderlaget är alltid begränsat till högst 250 000 värden.
Gränserna kan justeras utan att ta bort minnesbegränsningen:

```powershell
python scripts/inspect_rasters.py data/ --exact-pixel-limit 5000000 --sample-pixels 500000
```

### Läs en enda rasterpixel

Den generella punktläsaren transformerar WGS84 till GeoTIFF-filens eget CRS och
läser ett enda 1 × 1-fönster. CRS hårdkodas inte och filen öppnas alltid read-only:

```powershell
python scripts/sample_raster.py src/data/kind/Gran_andel.tif `
  --latitude 59.412 `
  --longitude 18.132
```

`ForestRasterDataSource` kan lägga ett skogsraster i en
`StaticHabitatFeatures`-modell. För `Gran_andel.tif` är säkert standardläge att
bevara råvärdet utan semantisk konvertering:

```python
from fungifind.data_sources import ForestRasterDataSource

gran_source = ForestRasterDataSource("src/data/kind/Gran_andel.tif")
snapshot = gran_source.get_features(location)
```

Inspectionen visar ett 0–100-intervall och filnamnet anger "andel", men källfilen
saknar enhet och bandbeskrivning. En preliminär omräkning till 0–1 måste därför
väljas explicit och förblir märkt som semantiskt ovaliderad:

```python
from fungifind.data_sources import (
    ForestRasterDataSource,
    ForestShareInterpretation,
    HybridHabitatDataSource,
    MockHabitatDataSource,
)

gran_source = ForestRasterDataSource(
    "src/data/kind/Gran_andel.tif",
    interpretation=ForestShareInterpretation.PROVISIONAL_ZERO_TO_HUNDRED_SHARE,
)
hybrid_source = HybridHabitatDataSource(gran_source, MockHabitatDataSource())
```

I hybridläget kommer `spruce_fraction` från det riktiga rastret och övriga
habitatfeatures tillfälligt från mockkällan. `FeatureSnapshot` och `ModelResult`
behåller provenance per feature. Den nuvarande confidence-formeln har fortfarande
en enda kvalitetsvikt för hela habitatsnapshoten och använder därför konservativt
mockkällans kvalitet tills en separat per-feature confidence-modell införs.

Se [docs/gran_andel.md](docs/gran_andel.md) för det observerade rådatakontraktet
och den uttryckliga semantiska begränsningen.

### Prova den riktiga trädslagsprofilen

Fyra konfigurerade instanser av samma generella rasterreader mappar gran, tall,
björk och övrigt löv till separata habitatfeatures:

```powershell
python scripts/sample_forest.py --latitude 59.412 --longitude 18.132
```

Kommandot visar råvärden, uttryckligen preliminära 0–1-fraktioner, deras
oförändrade summa, diagnostik för summa/missing/NoData samt CRS och pixelindex.
Ingen automatisk omnormalisering görs. Se
[docs/tree_species_rasters.md](docs/tree_species_rasters.md) för jämförelsen av
de fyra rasterfilerna.

### Prova råa skogsstrukturfeatures

HGV, Vegkvot och GY läses via samma generella `RasterPointReader`, men lämnas
semantiskt otolkade och används inte i scoring:

```powershell
python scripts/sample_habitat.py --latitude 59.412 --longitude 18.132
```

Kommandot visar riktiga trädslagsvärden, råa strukturvärden, provenance, NoData,
CRS/pixel per raster och grid alignment. Se
[docs/forest_structure_rasters.md](docs/forest_structure_rasters.md) för inspection
och jämförelse med trädslagsgridet.

### Prova klassad statisk fuktighetspotential

`SLUMarkfuktighetKlassad.tif` läses som en långsiktig/statisk hydrologisk
fuktighetspotential. Den är inte ett mått på dagens markfuktighet:

```powershell
python scripts/sample_habitat.py --latitude 59.412 --longitude 18.132
```

CLI:t visar råklass, officiellt mappad etikett, semantic status, CRS, pixel och
NoData. Utan en explicit validerad klassmappning bevaras råklassen endast i
provenance och påverkar inte scoring. Med den dokumenterade SLU-mappningen
aktiveras en separat `static_wetness`-komponent; dess artspecifika
svamppreferenser är fortfarande biologiskt preliminära. Dynamisk framtida
fuktighet hålls separat som `estimated_current_soil_moisture_index`.

Se [docs/static_wetness_raster.md](docs/static_wetness_raster.md) för inspection,
klasskälla, verkliga punktuppslag och scoringeffekt.

## Rekommenderade nästa steg

1. Granska de integrerade rasterlagrens semantik och spatiala konsistens i ett
   litet geografiskt pilotområde.
2. Implementera senare en SMHI-adapter som räknar fram exakt definierade regnfönster och
   temperaturaggregat med korrekt observationstid och datakvalitet.
3. Lägg till raster-/API-kontrakttester, cache och tydlig hantering av punkter
   utanför datatäckning.
4. Först därefter: samla fynd och bakgrunds-/frånvaropunkter, undvik spatialt
   läckage, träna och kalibrera separata artmodeller och utvärdera dem geografiskt.
5. Exponera den stabila servicegränsen via FastAPI; PostGIS och frontend kan vänta
   tills analysmotorn och datatäckningen motiverar dem.
