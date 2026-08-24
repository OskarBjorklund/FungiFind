# SGD2 skogsstruktur: inspection och semantisk status

Inspectionen är minnesbegränsad och statistiken nedan är approximativ från 999 090
representativa pixlar per raster.

## Gemensam struktur för HGV, Vegkvot och GY

- CRS-WKT: SWEREF99 TM med inbäddad `AUTHORITY["EPSG","3006"]`.
- Rasterios strikta `to_epsg()` gav `None`; PyProj identifierar WKT:n som EPSG:3006.
- Upplösning: 10 × 10 CRS-enheter.
- Dimensioner: 80 000 × 160 000.
- Bounds: ungefär `(200000, 6100000, 1000000, 7700000)`.
- Transform: `(200000, 10, 0, 7700000, 0, -10)`.
- Dtype: `int16`; NoData: `-1`.
- Tiled LZW, block 128 × 128, inga overviews.
- Inga units eller bandbeskrivningar.
- Taggar: `DataType=Generic`, `AREA_OR_POINT=Area`, `COMPRESSION=LZW`,
  `INTERLEAVE=BAND`.

| Raster | Min | Max | P1 | P5 | P25 | P50 | P75 | P95 | P99 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| HGV | 0 | 510 | 0 | 0 | 0 | 92 | 148 | 214 | 253 |
| Vegkvot | 0 | 100 | 0 | 0 | 0 | 45 | 77 | 96 | 99 |
| GY | 0 | 112 | 0 | 0 | 0 | 9 | 20 | 33 | 41 |

Ingen lokal metadata bekräftar meter, procent eller m²/ha. Råvärden lagras därför
i provenance med `interpreted_value=None` och semantic status `unvalidated`.

## Jämförelse med trädslagsrastren

Trädslagsrastren använder EPSG:25833, 12,5 m, dimensionerna 52 600 × 123 200,
bounds `(265000, 6132500, 922500, 7672500)` och en annan transform. Grid alignment
mellan de två rasterfamiljerna är därför `different`. Varje raster transformerar
och indexerar WGS84-punkten självständigt.

## Verifierat punktuppslag

För WGS84 `59.412, 18.132` gav samtliga tre SGD2-raster följande:

| Feature | Råvärde | Tolkat värde | Pixel | NoData |
|---|---:|---|---|---|
| `forest_mean_height` / HGV | 0 | `None` | row=110988, col=47774 | false |
| `vegetation_ratio` / Vegkvot | 0 | `None` | row=110988, col=47774 | false |
| `basal_area` / GY | 0 | `None` | row=110988, col=47774 | false |

Råvärdet 0 är giltigt i förhållande till NoData=-1, men dess betydelse tolkas inte.
De tre strukturgriden är `exact` alignade med varandra. GY:s serialiserade
vänsterkoordinat skiljer cirka 6×10⁻¹¹ från de övriga; detta behandlas som
flyttalsbrus genom kanoniskt EPSG och avrundade transformkoefficienter. Den
samlade alignmenten mot trädslagsrastren är fortfarande `different`.

Kantarellresultatet är exakt oförändrat när strukturprovenance läggs till:
`habitat_score=0.927532`, `fruiting_score=0.986`, `final_score=0.950919` och
`confidence=0.41` både före och efter. Inga strukturfeatures ingår i scoring.
