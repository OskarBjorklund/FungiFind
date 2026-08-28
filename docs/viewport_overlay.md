# Viewportbaserat områdesindex

FungiFind kan visa ett begränsat, dynamiskt rutnät över den aktuella kartvyn.
Lagret är ett **heuristiskt lämplighetsindex 0–1**, inte en sannolikhet för fynd.
Det använder endast `production_v1`; experimentell v2 och det separata
markfuktighetsindexet ingår inte i overlayn.

Punktanalysen via `GET /api/score` är fortsatt ett separat flöde. Ett kartklick
hämtar den fulla punktförklaringen och påverkas inte av områdeslagrets cache,
avbrott eller rendering.

## API

`GET /api/viewport` tar följande queryparametrar:

| Parameter | Format | Regel |
| --- | --- | --- |
| `west`, `south`, `east`, `north` | WGS84-tal | ändliga, ordnade koordinater |
| `species` | stödd artkod | samma arter som punkt-API:t |
| `date` | `YYYY-MM-DD` | valfri; aktuell UTC-dag om den saknas |
| `resolution_m` | meter | exakt `25`, `50`, `100` eller `200` |

Exempel:

```powershell
Invoke-RestMethod "http://localhost:8000/api/viewport?west=18.245&south=59.158&east=18.249&north=59.162&species=cantharellus_cibarius&date=2026-08-27&resolution_m=50"
```

Det kompakta alternativet `bbox=west,south,east,north` och `resolution` stöds
också för bakåtkompatibilitet, men formerna får inte blandas i samma request.

Backend transformerar bboxens hörn till SWEREF 99 TM (`EPSG:3006`) och alignar
rutnätet mot en global, deterministisk multipel av cellstorleken. Samma cell får
därför samma polygon och `cell_id` oavsett vilken överlappande viewport som
begärde den. Om fler än 10 000 celler skulle skapas dubblas upplösningen
deterministiskt tills taket hålls; metadata returnerar både
`requested_resolution_m` och `actual_resolution_m`.

Ytterligare skydd:

- högst 100 km projicerad spännvidd per sida,
- högst 10 000 km² projicerad bboxarea,
- högst 10 000 alignade gridceller,
- tydliga 422-fel: `invalid_bbox`, `bbox_too_large`, `invalid_resolution`,
  `invalid_species` och `invalid_date`.

Svaret är en kompakt GeoJSON `FeatureCollection`. Varje feature är en riktig
fyrkantig cellpolygon med endast:

- `final_index`, `habitat_index` och `fruiting_index` från `production_v1`,
- `data_confidence`, som är datakomplettering/källkvalitet och inte statistisk
  osäkerhet,
- stabilt `cell_id` och explicit `eligibility = "eligible"`.

Exkluderade habitat och celler utan datatäckning returneras inte som index 0.
De utelämnas helt och räknas separat i metadata som `excluded_cell_count` och
`no_data_cell_count`. `feature_count` är därför antalet faktiskt ritbara celler.

## Batchvägen

Viewportutvärderaren anropar inte punktservicen cell för cell. Den har en egen
intern batchväg:

1. alla cellcentra skapas och transformeras samlat,
2. NMD och SLU-markfuktighet läses som eligibility-preflight,
3. exkluderade celler stoppas före trädslagsraster, SGU, DEM och väder,
4. varje berört rasterblock för återstående celler läses en gång per raster,
5. öppna read-only rasterdataset och CRS-transformers återanvänds trådsäkert,
6. SGU:s immutable GeoPackage-anslutning återanvänds,
7. eligible celler grupperas per snappad MESAN-gridpunkt och datum,
8. 30-dygnsfönstret läses en gång per unik MESAN-grupp,
9. endast produktionsmotorn körs och GeoJSON byggs lokalt innan cachepublicering.

Ett avbrutet eller felande batchjobb publicerar aldrig ett partiellt cachesvar.
Backendens TTL/LRU-cache har högst 32 entries. Nyckeln innehåller art, datum,
faktisk upplösning, alignade SWEREF-gränser, modellversion och konfigurationsversion.
Historiska datum har 3 600 sekunders TTL; aktuell eller framtida dag 60 sekunder.

## Klientbeteende

Områdeslagret är aktiverat i gränssnittet men gör inga anrop under konfigurerad
minzoom, standard 11. Upplösningen väljs före eventuell backend-förgrovning:

| Kartzoom | Begärd upplösning |
| --- | --- |
| 11–<13 | 200 m |
| 13–<14 | 100 m |
| 14–<15 | 50 m |
| ≥15 | 25 m |

Vid `moveend` och `zoomend` expanderas MapLibre-bboxen 25 procent åt varje sida.
Anropet debouncas 320 ms. En ny rörelse avbryter både väntande timer och pågående
`fetch`, tömmer gamla polygoner och höjer ett generationsnummer. Ett sent svar
från en äldre generation kan därför aldrig ritas.

En klient-LRU med 12 entries kan återanvända tidigare data när den expanderade
nya bboxen ryms i en cachad `coverage_bbox` med samma art, datum och begärda
upplösning. MapLibre använder en GeoJSON source och en `fill`-layer med riktiga
celler, inte en heatmap. Kontrollen visar av/på, opacitet, loading, fel,
cellantal och faktisk meterupplösning.

Habitatgridets 25–200 meter gör inte vädret lika finmaskigt. MESAN-gridet är
cirka 2,5 km, så många habitatceller delar exakt samma väderhistorik och
30-dygnsaggregat. Skillnaden är avsiktlig och redovisas som antalet unika
MESAN-punkter i metadata.

Frontendkonfiguration:

| Variabel | Standard |
| --- | --- |
| `NEXT_PUBLIC_VIEWPORT_MIN_ZOOM` | `11` |
| `NEXT_PUBLIC_VIEWPORT_DEBOUNCE_MS` | `320` |
| `NEXT_PUBLIC_VIEWPORT_PREFETCH_FRACTION` | `0.25` |
| `NEXT_PUBLIC_VIEWPORT_CACHE_ENTRIES` | `12` |

## Benchmark och profil

Kör den reproducerbara profilen från repositoryts rot:

```powershell
$env:PYTHONPATH="src"
python scripts/benchmark_viewport.py --output reports/viewport_overlay_benchmark.json
```

Mätningen 2026-08-28 använde centrum `59.160136, 18.247348`, kantarell och datum
2026-08-27. En WGS84-rektangel som omsluter en projicerad kvadrat kan ge fler
kantceller än det nominella `storlek/upplösning`-talet.

| Fall | Celler | Eligible | Excluded | Unika MESAN | Cold | Warm | Cold celler/s |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1×1 km, 50 m | 529 | 512 | 17 | 1 | 3,39 s | 3,07 s | 155,91 |
| 5×5 km, 100 m | 3 080 | 2 825 | 255 | 1 | 7,91 s | 7,36 s | 389,37 |
| 10×10 km, 200 m | 3 136 | 2 566 | 570 | 1 | 8,57 s | 7,26 s | 366,00 |

Den fulla maskinläsbara rapporten finns i
[`reports/viewport_overlay_benchmark.json`](../reports/viewport_overlay_benchmark.json).
Warm är ett andra evaluatorpass med öppna resurser; en faktisk backend-cacheträff
är endast ett LRU-uppslag och behöver ingen ny evaluatorpass. För de tre fallen
låg ungefärlig källtid på NMD 74–199 ms, SLU 132–499 ms, trädslagsrastren
2,69–4,57 s, SGU 0,53–3,43 s och DEM 5–30 ms. MESAN låg på 36–41 ms och
produktionsscoring på 21–125 ms. Den separata moisture-modellen körs inte alls i
overlayn (`0 ms`), eftersom `production_v1` inte behöver dess experimentella
aktuella fuktindex. Verkliga flaskhalsar är därmed trädslagsraster och, för stora
viewports, SGU-punktuppslagen.

## Test och avgränsning

Backendtester täcker global alignment, stabil cellidentitet, auto-förgrovning,
storleksgränser, eligibility-first, produktionsfält, cacheidentitet, TTL/LRU,
felkoder och att felande jobb inte cachas. Frontendtester täcker zoomband,
bbox-expansion, debounce, abort, generationsskydd, LRU, minzoom och felstatus.

```powershell
python -m pytest
python -m ruff check .
cd frontend
npm test
npm run lint
npm run build
```

Versionen innehåller ingen XYZ-tjänst, ingen rikstäckande precompute, ingen
SNOW1G-data, ingen ML-modell och inga konton.
