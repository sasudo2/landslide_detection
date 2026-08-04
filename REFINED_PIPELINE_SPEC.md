# Nepal Landslide Pipeline Refined Specification

This document captures the finalized requirements for a Kaggle + Hugging Face workflow using three Python notebooks:

1. `planet_order_creation.ipynb`
2. `incident_download.ipynb`
3. `candidate_detection.ipynb`

## Global Rules

1. Input CSV path:
   `/kaggle/input/datasets/sanjayashrestha123/landslide-reproted/landslides_from_2018_to_2026.csv`
2. CSV columns:
   `id,title,incident_on,min_lon,min_lat,max_lon,max_lat`
3. Incident filtering must use row index range (`start_idx`, `end_idx`).
4. **Default Planet acquisition mode is monthly Mosaic composites**
   (`USE_MOSAIC_COMPOSITES=True` in both Notebook 1 and Notebook 2 - keep in sync).
   Planet after/before imagery is fetched directly from the Mosaics/quads API in
   `incident_download.ipynb`; `planet_order_creation.ipynb` performs no ordering in this
   mode. Rules 5-8 below describe the legacy per-scene ordering mode, used only when
   `USE_MOSAIC_COMPOSITES=False`.
5. Cloud threshold for before and after imagery in legacy per-scene mode: `< 5%`.
6. Legacy per-scene before window: up to 6 months before incident date.
7. Legacy per-scene post window: up to 1 month after incident date for ancillary GEE/SAR
   data (`incident_download.ipynb`, applies regardless of acquisition mode). Planet's own
   after-image search (`planet_order_creation.ipynb`, legacy mode only) widens this to 2
   months (`POST_DAYS=60`) since a 1-month window too often has no <5%-cloud PlanetScope
   scene, especially during/after monsoon cloud cover.
8. Date prioritization (legacy per-scene mode, and GEE/SAR selection in both modes):
   closest date first, then least cloud.
9. Hugging Face dataset: `sasudo2/landslides`.
10. Storage layout:
    - `raw_images/raw_incidents/incident_{ID}/` for raw incident rasters
    - `raw_images/order_log.csv` for order tracking and dedup (legacy mode only)
    - `candidates/` for candidate outputs

## Notebook 1: `planet_order_creation.ipynb`

### Required behavior

**Default (`USE_MOSAIC_COMPOSITES=True`)**: this entire notebook is a no-op - it prints a
message and exits, since Mosaic composites are fetched directly in
`incident_download.ipynb` with no order to submit or poll.

**Legacy per-scene mode (`USE_MOSAIC_COMPOSITES=False` only)** - items 1-7 below apply:

1. Create Planet orders for after imagery.
2. Create Planet before orders only if user enables it.
   - Default: before ordering is OFF.
3. Search Planet imagery with cloud `< 5%` and configured date windows.
4. Support multi-scene ordering per incident (downstream will mosaic).
5. Deduplicate by reading `raw_images/order_log.csv` from Hugging Face.
6. Log all outcomes (created/skipped/failed) with full metadata and errors.
7. Use robust API error reporting that includes full response body for 4xx/5xx.

### Tracking fields in `order_log.csv`

Use all practical fields for traceability:

- `incident_id`
- `title`
- `incident_on`
- `min_lon`
- `min_lat`
- `max_lon`
- `max_lat`
- `row_index`
- `order_type` (`after` or `before`)
- `before_enabled`
- `order_name`
- `order_id`
- `order_state`
- `scene_count`
- `created_at`
- `error`

## Notebook 2: `incident_download.ipynb`

### Required behavior

1. Read Hugging Face files (and, when `USE_MOSAIC_COMPOSITES=False`, live Planet order
   state) to only process missing artifacts.
2. Acquire Planet after/before imagery via one of two modes, selected by the
   `USE_MOSAIC_COMPOSITES` toggle (must match the same flag in
   `planet_order_creation.ipynb`):
   - **`True` (default - monthly Mosaic composites)**: fetch directly from Planet's
     Mosaics/quads API, no Orders API involved at all. `find_after_mosaic()` searches the
     incident's month then forward (the slide must already have happened);
     `find_before_mosaic()` searches backward starting one month before the incident. All
     quads covering the incident AOI are downloaded, mosaicked if there is more than one,
     and window-clipped to the exact AOI bbox. Mosaic quads are delivered in their own
     native CRS (typically Web Mercator, EPSG:3857, not WGS84) - the AOI bbox must be
     reprojected to the quad's CRS (`rasterio.warp.transform_bounds`) before computing the
     clip window, otherwise the window silently rounds to 0 pixels and crashes on write.
     No per-scene UDM2 masking is needed here since Planet's own compositing already
     removes cloud/shadow and normalizes color across constituent scenes.
   - **`False` (legacy per-scene ordering)**: download Planet after/before imagery from
     preordered incidents created by `planet_order_creation.ipynb`
     (per `order_log.csv`/live order state). Each scene's analytic SR asset must be
     paired (by filename prefix) with its delivered UDM2 usable-data-mask asset from the
     same `analytic_sr_udm2` bundle, and any pixel flagged cloud/shadow/haze/snow (UDM2
     band 1 ≠ clear) must be zeroed out **before** mosaicking - Planet's scene-level
     `cloud_cover` filter is computed over the whole scene footprint, not the tiny
     clipped incident AOI, so a nominally "clean" scene can still have cloud sitting
     directly over the incident.
3. If multiple assets/scenes/quads exist for an incident, mosaic to one:
   - `incident_{ID}_after.tif`
4. Before imagery rules:
   - Download Planet before if available as `incident_{ID}_planet_before.tif`
   - Download GEE before as `incident_{ID}_gee_before.tif`
   - If Planet before is unavailable, GEE before is mandatory fallback
   - If both are available, keep both files
5. Download ancillary data for every processed incident:
   - `incident_{ID}_sar_pre.tif` (VV + VH)
   - `incident_{ID}_sar_post.tif` (VV + VH)
   - `incident_{ID}_slope.tif`
   - `incident_{ID}_gee_aspect.tif`
6. GEE before selection:
   - cloud `< 5%`
   - default 6-month window before incident
   - configurable pre-window (`pre_days`)
   - prioritize closest date first, then least cloud
7. No resampling here; resampling is handled in Notebook 3.
8. Upload to:
   - `raw_images/raw_incidents/incident_{ID}/`

## Notebook 3: `candidate_detection.ipynb`

### Required behavior

1. Load incident rasters from `raw_images/raw_incidents/incident_{ID}/`.
2. Resample all inputs to GEE sampling size in this notebook.
3. Optical IR-MAD:
   - Use common 4-band subset only: Blue, Green, Red, NIR
   - Do NOT include SCL as IR-MAD input
4. SAR IR-MAD:
   - Use VV and VH for both pre and post (4 SAR inputs total)
5. Implement robust IR-MAD using a maintained library approach, with stable fallback path if dependency is unavailable.
6. Produce optical and SAR chi-square/confidence maps separately.
7. NDVI-loss fusion: compute NDVI (before) minus NDVI (after), clip to >= 0 (vegetation
   loss only), percentile-normalize to `[0,1]`, and fuse it in as a third independent
   evidence channel alongside the optical/SAR IR-MAD confidences via an N-way geometric
   mean (only whichever of the three channels are available for that incident). This
   targets the actual landslide spectral signature (vegetation stripped, bare soil/debris
   exposed) instead of IR-MAD's raw spectral distance, which fires equally for any
   optical change (crop harvest, phenology, cloud residue).
8. Apply slope masking.
9. Run SLIC using feature stack:
   - fused confidence
   - normalized after-image texture channels
   - normalized slope
10. Bonferroni-corrected significance gate: for each SLIC segment, compute a z-score from
    the CLT approximation of its aggregate IR-MAD chi-square evidence (sum of chi divided
    by its expected mean/variance under the no-change null), and require
    `z >= norm.ppf(1 - FWER_ALPHA / n_segments)` (default `FWER_ALPHA=0.05`) in addition to
    the percentile-based hysteresis threshold below. Without this, the top-percentile
    threshold alone always produces a "detection" in every incident regardless of whether
    any real change occurred, since the maximum of many noisy per-segment scores drifts
    toward 1 purely from sample size.
11. Use dual-threshold hysteresis on confidence probabilities, ANDed with the
    significance gate from item 10.
12. Extract connected blobs, filter by area/elongation, rank severity from IR-MAD confidence/chi-square, keep top-N.
13. Save candidate chips/masks/metadata under `candidates/`.

## Naming Specification (Final)

Per incident folder:

- `incident_{ID}_after.tif`
- `incident_{ID}_planet_before.tif`
- `incident_{ID}_gee_before.tif`
- `incident_{ID}_sar_pre.tif`
- `incident_{ID}_sar_post.tif`
- `incident_{ID}_slope.tif`
- `incident_{ID}_gee_aspect.tif`

## Acceptance Checklist

1. Row index filtering works in Notebooks 1 and 2.
2. Cloud threshold `< 5%` is enforced for before and after selection (per-scene mode).
3. Before ordering default is OFF in Notebook 1 (per-scene mode).
4. Duplicate orders are prevented via `raw_images/order_log.csv` (per-scene mode).
5. Multi-scene orders/quads are mosaicked in Notebook 2.
6. Planet-before and GEE-before are both preserved when available.
7. GEE-before always exists if Planet-before is missing.
8. SAR pre/post VV+VH, slope, and aspect are generated for processed incidents.
9. Notebook 3 uses IR-MAD optical (4 bands) and IR-MAD SAR (VV/VH).
10. SCL is used only for masking, never as IR-MAD feature input.
11. Candidate outputs are written under `candidates/`.
12. Re-running pipeline is idempotent (skip behavior via HF logs/files).
13. `USE_MOSAIC_COMPOSITES` is set identically in both Notebook 1 and Notebook 2.
14. When `USE_MOSAIC_COMPOSITES=False`, every Planet SR asset is masked against its
    paired UDM2 asset before mosaicking (no unmasked cloud/shadow/haze/snow pixels).
15. When `USE_MOSAIC_COMPOSITES=True`, `MOSAIC_NAME_TEMPLATE` has been verified against
    the account's actual Planet plan mosaic series names (see self-check in cell 3).
16. Mosaic-clip AOI bounds are reprojected to the mosaic quad's native CRS before
    windowing (never window with raw WGS84 lon/lat against a non-4326 raster).
17. Notebook 3's fused confidence includes an NDVI-loss (vegetation-stripping) channel
    whenever 4-band optical (NIR present) is available, not just IR-MAD confidences.
18. Notebook 3 candidates only come from segments that pass the Bonferroni-corrected
    significance gate (item 10 above), not from percentile ranking alone.

## Addendum (2026-08-04): Monthly Mosaic composites

Goal shifted from before/after change-detection comparison toward reliably collecting a
clear image per known incident, while still keeping both before/after images and IR-MAD
change detection. Real downloaded per-scene chips showed cross-scene color mismatch,
unmasked cloud/haze blobs, and jagged mosaic seams (see `landslide_pipeline.md` repo
memory for the full pixel-level diagnosis). Planet's monthly Mosaic composites solve this
at the source (Planet does its own seam-blending, radiometric normalization, and cloud
exclusion when building each mosaic), so both before/after acquisition now default to the
Mosaics/quads API via `USE_MOSAIC_COMPOSITES=True`, with the original per-scene
Orders-API path (including the UDM2 masking fix) fully preserved as a fallback via the
same toggle in both Notebook 1 and Notebook 2. `candidate_detection.ipynb` (Notebook 3)
requires no changes, since both acquisition modes write to the same output filenames.

## Addendum (2026-08-04): Mosaic-clip CRS fix + candidate-detection false-positive reduction

**Mosaic-clip bug fix**: `download_mosaic_clip()` in Notebook 2 was windowing raw WGS84
lon/lat AOI bounds directly against the mosaic quad's native transform. Planet mosaic
quads are delivered in Web Mercator (EPSG:3857), not EPSG:4326, so this produced a
near/exactly-0-pixel window and crashed with "Attempt to create 0x0 dataset is illegal"
on every incident processed in composite mode. Fixed by reprojecting the AOI bbox to the
quad's actual CRS (`rasterio.warp.transform_bounds`) before computing the window, with an
explicit error if the corrected window is still empty (genuine no-overlap case).

**Candidate-detection false positives / poor localization**: after live testing, the user
reported the change-detection candidates had too many false positives and imprecise
localization. Root cause was two structural gaps in Notebook 3, not the core bi-temporal
approach itself (which remains necessary - the incident zone alone doesn't localize a
slide, per the design principle above; single-image detection was explicitly ruled out):
1. `HIGH_CONF_PERCENTILE`/`LOW_CONF_PERCENTILE` thresholding alone always produced a
   "detection" in every incident, since the maximum of many independent per-segment
   change scores drifts toward 1 by chance as segment count grows, even with zero real
   change anywhere in the AOI. Fixed with the Bonferroni-corrected significance gate
   (Notebook 3, required behavior item 10 and Acceptance Checklist item 18).
2. Raw-band IR-MAD treats any spectral difference (crop harvest, phenology, cloud
   residue) as equally suspicious as real landslide change. Fixed by fusing in an
   NDVI-loss (vegetation-stripping) channel - the actual landslide spectral signature -
   alongside the IR-MAD confidences (Notebook 3, required behavior item 7 and Acceptance
   Checklist item 17).

Not yet addressed (flagged as follow-up, out of scope for this addendum): SAR
orbit-direction matching for pre/post scene pairs, an explicit susceptibility/road/water
exclusion mask beyond the existing slope mask, and explicit before/after co-registration
verification.
