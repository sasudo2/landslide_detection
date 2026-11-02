# Landslide susceptibility detection

This repository processes reported landslide incidents into standardized raster inputs and candidate chips for downstream review.

The active processing flow is implemented in:
1. `incident_download.ipynb` (Planet quarterly composite download + GEE support layers)
2. `candidate_detection.ipynb` (change-based candidate extraction and candidate uploads)

`planet_order_creation.ipynb` is not part of the current flow and can be ignored.

Hugging Face dataset repo `sasudo2/landslides` is the storage backend.

## Current status

The incident download logic uses precomputed **Planet quarterly mosaic composites** as the primary optical source:
- before/after optical imagery is fetched directly from Planet's Mosaics/quads API per incident (no per-scene order submission or polling),
- Planet's own compositing already removes cloud/shadow and normalizes color across scenes,
- no per-scene order creation, order log, per-incident quality-report, or run manifest is used anymore.

## Notebook 1: incident_download.ipynb

Purpose:
- For each incident AOI, produce before and after optical rasters plus support layers.

Key behavior:
1. Read incident rows from CSV and clamp AOI extent to bounded size (`MAX_AOI_DEG`).
2. For each side (before/after), select a precomputed quarterly Planet mosaic from Nepal's **dry season** (`DRY_SEASON_MONTHS`, default Oct–Mar, i.e. Q4 and Q1) to avoid monsoon cloud cover:
   - after side: the nearest published dry-season quarter strictly after the incident's quarter, trying up to `MOSAIC_MAX_DRY_PERIODS_FORWARD` dry quarters,
   - before side: the nearest published dry-season quarter strictly before the incident's quarter, trying up to `MOSAIC_MAX_DRY_PERIODS_BACK` dry quarters,
   - the incident's own quarter is never used, so neither composite mixes pre- and post-event pixels,
   - mosaic discovery uses the naming pattern `MOSAIC_NAME_TEMPLATE` (`global_quarterly_{year}q{quarter}_mosaic`).
3. For each selected mosaic:
   - query quads intersecting the AOI (bbox),
   - download the quad GeoTIFFs,
   - merge them if there is more than one,
   - window-clip to the exact incident bbox.
4. Write the Planet outputs:
   - `incident_{ID}_after.tif`,
   - `incident_{ID}_planet_before.tif`.
5. Download GEE support layers when available:
   - optical before fallback (`incident_{ID}_gee_before.tif`: most recent cloud-free Sentinel-2 scene from dry-season months within `S2_DRY_SEASON_LOOKBACK_DAYS` before the incident),
   - SAR pre and post (`VV,VH`),
   - slope and aspect.
6. Upload all downloaded TIFFs to `raw_images/raw_incidents/incident_{ID}/` on Hugging Face in batched commits.
7. Upload a run-level `download_log.csv` summarizing per-incident outcomes.

### Authentication and environment

Required:
- `Lokesh_planet` secret holding the Planet API key (for the Mosaics/quads API)
- `huggingface_token` secret for Hugging Face uploads

Optional (for GEE service-account init):
- `GEE_PROJECT`
- `GEE_SERVICE_ACCOUNT`
- `GEE_KEY_PATH` (or `gee_json_key` secret)

## Output layout

Primary working outputs are generated under:
- `/kaggle/working/raw_incidents/`

Per incident:
- `incident_{ID}/incident_{ID}_after.tif`
- `incident_{ID}/incident_{ID}_planet_before.tif`
- `incident_{ID}/incident_{ID}_gee_before.tif` (when available)
- `incident_{ID}/incident_{ID}_sar_pre.tif` (when available)
- `incident_{ID}/incident_{ID}_sar_post.tif` (when available)
- `incident_{ID}/incident_{ID}_slope.tif` (when available)
- `incident_{ID}/incident_{ID}_gee_aspect.tif` (when available)

Run-level artifacts:
- `download_log.csv`

## Notebook 2: candidate_detection.ipynb

Purpose:
- Build ranked candidate chips per incident from multi-source raster evidence.

Current behavior highlights:
- Uses available before/after/support rasters (treating the optical pair as RGB-first since the quarterly composites are color/visual mosaics).
- Produces candidate chip outputs and metadata.
- Uploads candidate artifacts to Hugging Face in batched commit operations.
- Uploads `candidate_status.csv` and `candidate_metadata.csv` together in one commit.

## Related documentation

- `REFINED_PIPELINE_SPEC.md`: authoritative requirements/spec checklist.
- `MOSAIC_SOURCE_QUALITY_RUNBOOK.md`: operational runbook for quarterly composite selection.
- `landslide_workflow.md`: legacy scientific workflow notes and broader methodology context.