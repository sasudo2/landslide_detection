# Landslide susceptibility detection

This project identifies landslide susceptibility using risk factors collected from
satellite images (Planet.com PlanetScope optical, Sentinel-2 optical + Sentinel-1 SAR
via Google Earth Engine, and SRTM DEM derivatives) for reported landslide incidents
from the Bipad portal.

## Workflow (three Kaggle notebooks)

The pipeline is implemented as three sequential Kaggle notebooks. Hugging Face
(`sasudo2/landslides`, dataset repo) is the single source of truth for both raw imagery
and candidate outputs — there is no separate second repo. Full behavioral spec,
naming rules, and the acceptance checklist live in
[`REFINED_PIPELINE_SPEC.md`](REFINED_PIPELINE_SPEC.md); this section is a short summary.

### 1. `planet_order_creation.ipynb`
Creates Planet Orders API (v2) orders for **after** imagery (always) and, if
`ORDER_BEFORE=True` (default `False`), **before** imagery. Incidents are selected via a
row-index range (`START_IDX`/`END_IDX`) over the input CSV. Scenes are searched with
Planet's Data API quick-search (`PSScene`, `analytic_sr_udm2` bundle, cloud cover `< 5%`,
before window up to 180 days pre-incident, after window up to 30 days post-incident) and
ranked by (closest date, then least cloud). Up to `MAX_SCENES_PER_ORDER` scenes are
included per order to support multi-scene mosaicking downstream. All order attempts
(created/skipped/failed) are logged to `raw_images/order_log.csv` on Hugging Face, which
is also read back on each run to avoid re-ordering the same `(incident_id, order_type)` —
only genuinely `failed` attempts are retried.

### 2. `incident_download.ipynb`
Downloads the raster inputs needed for change detection, using the Hugging Face file
inventory and live Planet order state to skip incidents/files already present:
- **After**: downloads the Planet `..._planet_after` order's result(s) and mosaics
  multi-scene orders into a single file.
- **Before**: downloads the Planet `..._planet_before` order if one exists (kept
  separately, never overwrites GEE-before); Sentinel-2 **GEE-before is always attempted**
  as a mandatory fallback (closest date first, then least cloud, `SCL`-based cloud
  filtering) so every incident has at least one before image.
- **SAR**: Sentinel-1 GRD VV+VH, closest pre-event and closest post-event scene.
- **Terrain**: SRTM-derived slope and aspect.

All files are uploaded to `raw_images/raw_incidents/incident_{ID}/` on Hugging Face, and
a `raw_images/download_log.csv` records per-incident upload status.

### 3. `candidate_detection.ipynb`
Runs bi-temporal **IR-MAD** (Iteratively Re-weighted Multivariate Alteration Detection)
change detection separately on the optical 4-band pair (before/after, common
Red/Green/Blue/NIR subset) and the SAR VV/VH pair, fuses the two confidence maps, applies
a slope mask (`>= 20°`), and segments with SLIC superpixels sized to a target ~30 m
physical footprint. A statistical hysteresis threshold (calibrated IR-MAD confidence,
high/low significance) turns the fused confidence map into a binary mask; connected
components are filtered by area/elongation, scored, and ranked. All rasters are
reprojected onto a common analysis grid (EPSG:4326, ~10 m — matching GEE's native
sampling) before IR-MAD so before/after/SAR/slope pixels are spatially aligned regardless
of each source's native CRS/resolution.

Outputs are uploaded under `candidates/` on Hugging Face:
- `candidates/incident_{ID}/candidate_{N}/incident_{ID}_candidate_{N}_{before,after,slope,mask}.tif`
  — per-candidate chips (bbox padded by 100 m), one folder per ranked candidate, including
  a clipped binary change-mask chip alongside before/after/slope. There is no
  whole-incident mask or JSON metadata file — only these per-candidate outputs.
- `candidates/candidate_status.csv` — per-incident run status; the presence of any file
  under `candidates/incident_{ID}/` is also used to skip incidents already processed on
  re-runs.

## Naming conventions (Hugging Face, repo `sasudo2/landslides`)

```
raw_images/
  order_log.csv
  download_log.csv
  raw_incidents/incident_{ID}/
    incident_{ID}_after.tif
    incident_{ID}_planet_before.tif   (optional, if a Planet before order succeeded)
    incident_{ID}_gee_before.tif      (mandatory fallback)
    incident_{ID}_sar_pre.tif
    incident_{ID}_sar_post.tif
    incident_{ID}_slope.tif
    incident_{ID}_gee_aspect.tif
candidates/
  candidate_status.csv
  incident_{ID}/candidate_{N}/
    incident_{ID}_candidate_{N}_before.tif
    incident_{ID}_candidate_{N}_after.tif
    incident_{ID}_candidate_{N}_slope.tif
    incident_{ID}_candidate_{N}_mask.tif
```

Planet order names follow `incident_{ID}_planet_after` / `incident_{ID}_planet_before`.

## Other components
- `landslide_annotator/` — a QGIS plugin for manual annotation of candidate chips
  (loads `incident_XXXX/candidate_YYYY` folder pairs, side-by-side canvas view with
  annotation buttons and CSV export). Independent of the 3-notebook pipeline above.
- `nepal-administrative-boundary-shapefiles/` — reference administrative boundaries.
- `landslide_workflow.md` — earlier scientific-rationale document (stage-by-stage
  why/how, references) covering the original GEE-only, index-fusion design. Some
  specifics there (e.g. change indices, SAR coherence, susceptibility priors) predate
  the current Planet + IR-MAD pipeline described above and in
  `REFINED_PIPELINE_SPEC.md`; treat `REFINED_PIPELINE_SPEC.md` as authoritative for the
  implemented pipeline.

## Work done
- Collection of all reported landslide cases from the Bipad portal.
- Full 3-notebook pipeline implemented: Planet order creation, incident download
  (Planet + GEE + SAR + DEM), and IR-MAD-based candidate detection with per-candidate
  chip export.

## Work under progress / not yet run on Kaggle
- End-to-end execution and validation of the 3-notebook pipeline against the full
  incident set (implementation is complete and statically validated, but not yet
  executed against live Planet/GEE/Hugging Face services in this session).
- SAM2 / segmentation refinement stage downstream of candidate detection (out of
  scope for the current 3 notebooks, per `REFINED_PIPELINE_SPEC.md`).
