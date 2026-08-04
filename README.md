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
**Only relevant when `USE_MOSAIC_COMPOSITES=False`** (see below) — when the toggle is
`True` (current default in both notebooks), this notebook's order-submission loop is a
no-op (it just prints a message), since Mosaic composites need no Orders API submission.
When switched to per-scene mode, it creates Planet Orders API (v2) orders for **after**
imagery (always) and, if `ORDER_BEFORE=True` (default `False`), **before** imagery.
Incidents are selected via a row-index range (`START_IDX`/`END_IDX`) over the input CSV.
Scenes are searched with Planet's Data API quick-search (`PSScene`, `analytic_sr_udm2`
bundle, cloud cover `< 5%`, before window up to 180 days pre-incident, after window up to
60 days post-incident - widened from the original 30-day spec since a 1-month window too
often had no <5%-cloud scene during/after monsoon season) and ranked by (closest date,
then least cloud). Up to `MAX_SCENES_PER_ORDER` scenes are included per order to support
multi-scene mosaicking downstream. All order attempts (created/skipped/failed) are logged
to `raw_images/order_log.csv` on Hugging Face, which is also read back on each run to
avoid re-ordering the same `(incident_id, order_type)` — only genuinely `failed` attempts
are retried.

### 2. `incident_download.ipynb`
Downloads the raster inputs needed for change detection, using the Hugging Face file
inventory to skip incidents/files already present. Planet before/after imagery is fetched
one of two ways, controlled by the `USE_MOSAIC_COMPOSITES` toggle (same flag in both
notebooks — keep them in sync):

- **`USE_MOSAIC_COMPOSITES=True` (current default)**: before/after imagery comes directly
  from Planet's Mosaics/quads API (monthly Basemap composites) — no order to submit or
  poll. `find_after_mosaic()` searches the incident's month, then forward, for the first
  published mosaic (the slide must already have happened); `find_before_mosaic()` searches
  backward starting one full month before the incident. All quads intersecting the
  incident's AOI are downloaded, mosaicked if more than one, and window-clipped to the
  exact AOI bbox, producing the same `incident_{ID}_after.tif` /
  `incident_{ID}_planet_before.tif` filenames as the per-scene path (so
  `candidate_detection.ipynb` needs no changes). Planet's own compositing already removes
  cloud/shadow/haze and normalizes color across constituent scenes, which is why this mode
  was adopted — the per-scene path below had visible cross-scene color mismatches and
  unmasked cloud blobs in exported chips. **Caveat**: `MOSAIC_NAME_TEMPLATE` (default
  `'global_monthly_{year}_{month:02d}_mosaic'`) must match your Planet plan's actual mosaic
  series naming — a one-time self-check prints a sample of real mosaic names available to
  the account right after Planet auth in cell 3; adjust the template if they don't match.
  Monthly analytic mosaics are also coarser (~4.77 m) than native PlanetScope (~3 m).
- **`USE_MOSAIC_COMPOSITES=False` (legacy per-scene path)**: downloads the Planet
  `..._planet_after` / `..._planet_before` order's result(s) (using live order state from
  `raw_images/order_log.csv`) and mosaics multi-scene orders into a single file. Each
  scene's analytic SR asset is paired with its delivered UDM2 usable-data-mask asset (same
  `analytic_sr_udm2` bundle) by filename prefix, and any pixel Planet's own UDM2
  classifier flags as cloud/shadow/haze/snow is zeroed out **before** mosaicking — this
  bundle was always ordered but the mask was previously never downloaded or applied, which
  is what caused the color-mismatch/cloud-blob chip quality issues in the first place
  (Planet's scene-level `cloud_cover` filter is computed over the whole scene footprint,
  not the tiny clipped incident AOI, so a "clean" scene can still have cloud sitting
  directly over the incident).

Regardless of mode: Sentinel-2 **GEE-before is always attempted** as a mandatory fallback
(closest date first, then least cloud, `SCL`-based cloud filtering) so every incident has
at least one before image; SAR (Sentinel-1 GRD VV+VH, closest pre/post scene) and terrain
(SRTM-derived slope/aspect) are downloaded the same way in both modes.

All files are uploaded to `raw_images/raw_incidents/incident_{ID}/` on Hugging Face, and
a `raw_images/download_log.csv` records per-incident upload status.

### 3. `candidate_detection.ipynb`
Runs bi-temporal **IR-MAD** (Iteratively Re-weighted Multivariate Alteration Detection)
change detection separately on the optical 4-band pair (before/after, common
Red/Green/Blue/NIR subset) and the SAR VV/VH pair. A third evidence channel - NDVI drop
from before to after (vegetation-loss/bare-soil exposure, the actual landslide spectral
signature, as opposed to IR-MAD's raw spectral distance which fires equally for any
change such as crop harvest or phenology) - is fused in alongside them via an N-way
geometric mean (whichever of optical/SAR/NDVI-loss are available for that incident).
The fused confidence map is masked to slope `>= 20°` and segmented with SLIC superpixels
sized to a target ~30 m physical footprint. A per-segment **Bonferroni-corrected
significance gate** (`FWER_ALPHA=0.05`, based on a CLT z-score over the segment's
aggregate IR-MAD chi-square evidence) is required in addition to the existing
high/low-percentile hysteresis threshold, so an incident with no statistically real
change anywhere now correctly produces 0 candidates instead of the old percentile-only
threshold always flagging the top percentile of whatever noise was present. Connected
components are filtered by area/elongation, scored, and ranked. All rasters are
reprojected onto a common analysis grid (EPSG:4326, ~10 m — matching GEE's native
sampling) before IR-MAD so before/after/SAR/slope pixels are spatially aligned regardless
of each source's native CRS/resolution.

Outputs are uploaded under `candidates/` on Hugging Face:
- `candidates/incident_{ID}/candidate_{N}/incident_{ID}_candidate_{N}_{before,after,slope,mask}.tif`
  — per-candidate chips (bbox padded by 300 m), one folder per ranked candidate, including
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
- UDM2 cloud/shadow/haze/snow masking added to the per-scene Planet download path.
- Switched Planet before/after acquisition to monthly Mosaic composites by default
  (`USE_MOSAIC_COMPOSITES=True`), with the old per-scene order path kept intact and
  selectable via the same toggle in both `planet_order_creation.ipynb` and
  `incident_download.ipynb`.
- Fixed a CRS-mismatch bug in the Mosaic-composite download path (`download_mosaic_clip`
  windowed with raw WGS84 degrees against Web-Mercator quads, crashing with "0x0 dataset"
  on every incident) by reprojecting the AOI bbox to the mosaic's native CRS first.
- Added a Bonferroni-corrected statistical significance gate and an NDVI-loss fusion
  channel to `candidate_detection.ipynb` to reduce false positives and improve
  localization (see Notebook 3 summary above).

## Work under progress / not yet run on Kaggle
- End-to-end execution and validation of the 3-notebook pipeline against the full
  incident set (implementation is complete and statically validated, but not yet
  executed against live Planet/GEE/Hugging Face services in this session).
- `MOSAIC_NAME_TEMPLATE` needs to be confirmed against the actual mosaic series name
  available on the account's Planet plan (a one-time self-check in
  `incident_download.ipynb` cell 3 prints real sample names for this) before the
  Mosaic-composite path can be trusted in production.
- Validate the new significance-gate/NDVI-fusion candidate detection changes against a
  batch of real incidents (false-positive rate and localization accuracy not yet
  re-measured after the fix).
- SAM2 / segmentation refinement stage downstream of candidate detection (out of
  scope for the current 3 notebooks, per `REFINED_PIPELINE_SPEC.md`).
