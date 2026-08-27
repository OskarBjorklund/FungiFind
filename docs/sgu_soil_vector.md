# SGU Jordarter 1:25 000--1:100 000

## Local source and inspection

Inspected source: `src/data/soil_type/jordarter25k_100k.gpkg` (8,695,873,536 bytes).
The requested `src/data/soil/` location was not present, so the adapter checks that path first
and then the existing `soil_type` location. The source is opened through a read-only,
immutable SQLite URI.

All feature layers use SWEREF 99 TM (EPSG:3006). Every layer has a registered
`gpkg_rtree_index`, and its RTree row count equals its feature count. `id INTEGER PRIMARY
KEY` is the FID in every layer.

| Layer | Geometry | Features | Bounds in EPSG:3006 | Class fields |
|---|---:|---:|---|---|
| `blockighet` | MultiPolygon | 1,712,465 | 267298.719, 6133486.500, 920265.490, 7587941.949 | `bl`, `bl_tx` |
| `grundlager` | MultiPolygon | 2,956,837 | 258912.734, 6121288.000, 920844.302, 7587941.949 | `jg2`, `jg2_tx` |
| `landform` | MultiPolygon | 97,106 | 312828.531, 6189840.197, 919508.052, 7587466.276 | `lf`, `lf_tx` |
| `linjer` | MultiLineString | 233,052 | 268913.125, 6134173.524, 919683.000, 7587769.882 | `sl`, `sl_tx` |
| `oversta_ytlager` | MultiPolygon | 2,242 | 356252.668, 6249089.865, 884473.682, 7493887.909 | `jy0`, `jy0_tx` |
| `punkter` | MultiPoint | 254,687 | 268521.562, 6133598.764, 916137.000, 7585390.931 | `sp`, `sp_tx` |
| `tackningskarta` | MultiPolygon | 896 | 258912.734, 6121288.000, 920844.302, 7587941.949 | mapping metadata |
| `underliggande_lager` | MultiPolygon | 10,666 | 279757.115, 6134308.145, 914966.928, 7507712.047 | `jd3`, `jd3_tx` |
| `ytlager` | MultiPolygon | 411,549 | 269179.188, 6133945.291, 919356.802, 7587437.917 | `jy1`, `jy1_tx` |

The file contains the core `gpkg_contents`, `gpkg_geometry_columns`,
`gpkg_spatial_ref_sys`, `gpkg_extensions`, tile-matrix tables, and the RTree tables/triggers.
It does not contain `gpkg_metadata`, `gpkg_metadata_reference`, `gpkg_data_columns`, or
`gpkg_data_column_constraints`.

The thematic polygon schemas follow
`id INTEGER`, `geom MULTIPOLYGON`, a `MEDIUMINT` code, a `TEXT(120)` label,
`kartering TEXT(20)`, `karttyp MEDIUMINT`, `symbol MEDIUMINT`,
`objectid MEDIUMINT`, `geom_area REAL`, and `geom_length REAL`. The code/label names are
the pairs shown in the table above. `linjer` has the same source fields but only
`geom_length`; `punkter` has neither geometry measure. `tackningskarta` instead has
`kartering`, `karttyp`, `insamling TEXT(254)`, `rek_skala MEDIUMINT`,
`und_hojd TEXT(254)`, `avslut_ar MEDIUMINT`, `rev_datum DATETIME`, `objectid`, and the
two geometry measures.

## Selected layer and official field semantics

SGU's product description defines `grundlager` as the obligatory, continuous layer for the
soil expected at mapping depth (approximately 0.5 m) and normally thicker than 0.5 m. It can
also identify exposed bedrock or bedrock with a thin/discontinuous soil cover. Therefore the
primary habitat feature uses:

- official code: `grundlager.jg2`
- official label: `grundlager.jg2_tx`
- source FID: `grundlager.id`
- supplementary raw attributes: `kartering`, `karttyp`, `symbol`, `objectid`, `geom_area`,
  and `geom_length`

There is no separately documented official underclass or soil-group column in
`grundlager`; `jg2` and `jg2_tx` are the verified classification pair. FungiFind's broad
`soil_group` is consequently explicit derived configuration, never presented as an SGU
field.

`ytlager` describes a thin or discontinuous surface layer, commonly averaging 0.5--1 m.
`oversta_ytlager` is an additional upper thin layer where two surface layers occur.
`underliggande_lager` describes known material below `grundlager`, is not comprehensive, and
has no single fixed depth. These layers are retained as separately documented source layers;
they are not silently merged into the primary soil feature.

## Official `jg2` mapping and separate derived group

The labels below are transcribed from the official value domain. The final column is a
FungiFind interpretation kept separate from the official class. Ambiguous or mixed sediments
are intentionally `other`.

| Code | Official label | Derived group |
|---:|---|---|
| 1 | Mossetorv | organic_peat |
| 5 | Kärrtorv | organic_peat |
| 6 | Gyttja | other |
| 9 | Svämsediment, ler--silt | fine_mineral |
| 10 | Svämsediment, sand | coarse_mineral |
| 13 | Flygsand | coarse_mineral |
| 16 | Gyttjelera (eller lergyttja) | other |
| 17 | Postglacial lera | fine_mineral |
| 19 | Postglacial finlera | fine_mineral |
| 21 | Sand | coarse_mineral |
| 22 | Postglacial grovlera | fine_mineral |
| 24 | Postglacial silt | fine_mineral |
| 26 | Finsand | coarse_mineral |
| 28 | Postglacial finsand | coarse_mineral |
| 31 | Postglacial sand | coarse_mineral |
| 33 | Svallsediment, grus | coarse_mineral |
| 34 | Klapper | coarse_mineral |
| 36 | Skaljord | coarse_mineral |
| 39 | Silt | fine_mineral |
| 40 | Glacial lera | fine_mineral |
| 43 | Glacial finlera | fine_mineral |
| 44 | Glacial grovlera | fine_mineral |
| 48 | Glacial silt | fine_mineral |
| 50 | Isälvssediment | other |
| 51 | Isälvssediment, sten--block | coarse_mineral |
| 55 | Isälvssediment, sand | coarse_mineral |
| 57 | Isälvssediment, grus | coarse_mineral |
| 62 | Svämsediment, grus | coarse_mineral |
| 66 | Blockmark | coarse_mineral |
| 75 | Torv | organic_peat |
| 79 | Postglacial grovsilt-finsand | fine_mineral |
| 81 | Talus (rasmassor) | coarse_mineral |
| 82 | Vittringsjord | other |
| 85 | Lera | fine_mineral |
| 86 | Lera--silt | fine_mineral |
| 87 | Sand--grus | coarse_mineral |
| 90 | Oklassat område | other |
| 91 | Vatten | water |
| 92 | Sten--block | coarse_mineral |
| 93 | Grusig morän | moraine |
| 95 | Sandig morän | moraine |
| 97 | Sandig-siltig morän | moraine |
| 98 | Morängrovlera | moraine |
| 99 | Moränfinlera | moraine |
| 100 | Morän | moraine |
| 101 | Moränlera | moraine |
| 200 | Fyllning | anthropogenic_fill |
| 322 | Fyllning, rödfyr | anthropogenic_fill |
| 823 | Fanerozoisk diabas | bedrock_or_thin_soil |
| 849 | Rösberg | bedrock_or_thin_soil |
| 850 | Sedimentärt berg | bedrock_or_thin_soil |
| 888 | Berg | bedrock_or_thin_soil |
| 890 | Urberg | bedrock_or_thin_soil |
| 1950 | Kalktuff | other |
| 2306 | Bleke och kalkgyttja | other |
| 2368 | Slamströmssediment, ler--block | other |
| 2372 | Flytjord eller skredjord | other |
| 8114 | Oklassat område, tidvis under vatten | other |
| 8175 | Torv, tidvis under vatten | organic_peat |
| 8186 | Lera--silt, tidvis under vatten | fine_mineral |
| 8802 | Älvsediment, grovsilt--finsand | fine_mineral |
| 8803 | Älvsediment, grus | coarse_mineral |
| 8804 | Älvsediment | other |
| 8806 | Älvsediment, ler--silt | fine_mineral |
| 8809 | Älvsediment, sand | coarse_mineral |
| 8814 | Älvsediment sten--block | coarse_mineral |
| 8919 | Vittringsjord, ler--silt | fine_mineral |
| 8937 | Svämsediment | other |
| 8950 | Vittringsjord, sand--grus | coarse_mineral |
| 9010 | Svämsediment, grovsilt--finsand | fine_mineral |
| 9060 | Glacial grovsilt--finsand | fine_mineral |
| 9147 | Morän omväxlande med sorterade sediment | moraine |
| 9191 | Glaciär | ice |
| 9299 | Morän, sand | moraine |
| 9336 | Morän, sten--block | moraine |
| 9792 | Moränlera eller lerig morän | moraine |
| 9794 | Lerig morän | moraine |
| 9950 | Skålla av sedimentärt berg | bedrock_or_thin_soil |
| 9960 | Skålla av sandsten | bedrock_or_thin_soil |

## Lookup and scoring boundary

Each WGS84 point is independently transformed to the selected layer CRS with `always_xy`.
The lookup first uses the layer RTree bounding-box index and then Shapely `covers` for exact
point-in-polygon testing. Boundaries count as matches. If overlaps produce multiple exact
matches, the lowest integer FID is selected deterministically and both candidate/match counts
remain in provenance.

The adapter populates `soil_type_code`, `soil_type_label`, and `soil_group`. It deliberately
does not populate the existing legacy `soil_type` scorer field. Thus SGU soil is currently
read, interpreted, and reported, but cannot affect score or confidence completeness.
