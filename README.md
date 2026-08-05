# Landslide susceptibility detection

This repository processes reported landslide incidents into standardized raster inputs and candidate chips for downstream review.

The active processing flow is implemented in:
1. `incident_download.ipynb` (Planet monthly-first download + daily fallback + GEE support layers)
2. `candidate_detection.ipynb` (change-based candidate extraction and candidate uploads)

Hugging Face dataset repo `sasudo2/landslides` is the storage backend.

## Current status

The incident download logic has been fully migrated to a single, deterministic path:
- monthly Planet mosaic candidate discovery and scoring first,
- daily Planet scene fallback only when monthly quality is insufficient,
- incident-level quality report JSON,
- rerun-safe manifest and run summary outputs.

No legacy toggle-based order-download branch is used in `incident_download.ipynb` anymore.

## Notebook 1: incident_download.ipynb

Purpose:
- For each incident AOI, produce before and after optical rasters plus support layers.

Key behavior:
1. Read incident rows from CSV and clamp AOI extent to bounded size.
2. Use incident-proximal monthly search:
   - before side evaluates months before the incident month,
   - after side evaluates incident month and later,
   - search width is controlled by `MONTH_RADIUS` (default 6 months around incident month).
3. For each side (before/after), evaluate monthly Planet mosaics in that directional incident-centered window.
4. For each monthly candidate:
   - discover by both naming patterns:
     - `global_monthly_{year}_{month:02d}_mosaic`
     - `ps_monthly_analytic_sr_subscription_{year}_{month:02d}_mosaic`
   - reject metadata that looks visual-like (`datatype` byte/uint8 or `bands < 4`)
   - query intersecting quads and evaluate AOI cloud gate from quad metadata
   - download quads, merge, clip to AOI
   - verify clipped file has at least 4 bands (real-file NIR check)
   - compute quality score from:
     - valid pixel coverage,
     - NIR sharpness,
     - bandwise contrast,
     - haze heuristic.
5. Select nearest valid monthly candidate first, then higher score as tie-breaker.
6. If no monthly candidate passes quality floor, fallback to daily Planet scene:
   - Data API quick-search for PSScene near incident date (side-aware),
   - create Orders API clip with `analytic_udm2` bundle,
   - download SR + UDM2 clip,
   - reject if UDM2 cloud percentage exceeds threshold,
   - verify SR file has at least 4 bands,
   - write fallback output.
7. Download support layers from GEE when available:
   - optical before fallback (`incident_{ID}_gee_before.tif` when scene exists),
   - SAR pre and post (`VV,VH`),
   - slope and aspect.
8. Write per-incident quality report JSON.
9. Update manifest after each incident for rerun-safe operation.
10. Write run summary CSV and final run summary JSON.

### Authentication and environment

Required:
- `PL_API_KEY`

Optional (for GEE service-account init):
- `GEE_PROJECT`
- `GEE_SERVICE_ACCOUNT`
- `GEE_KEY_PATH`

## Output layout

Primary working outputs are generated under:
- `/kaggle/working/raw_incidents/`

Per incident:
- `incident_{ID}/incident_{ID}_before.tif`
- `incident_{ID}/incident_{ID}_after.tif`
- `incident_{ID}/incident_{ID}_quality_report.json`
- `incident_{ID}/incident_{ID}_gee_before.tif` (when available)
- `incident_{ID}/incident_{ID}_sar_pre.tif` (when available)
- `incident_{ID}/incident_{ID}_sar_post.tif` (when available)
- `incident_{ID}/incident_{ID}_slope.tif` (when available)
- `incident_{ID}/incident_{ID}_gee_aspect.tif` (when available)

Run-level artifacts:
- `run_manifest.json`
- `run_summary.csv`
- `final_summary.json`

## Notebook 2: candidate_detection.ipynb

Purpose:
- Build ranked candidate chips per incident from multi-source raster evidence.

Current behavior highlights:
- Uses available before/after/support rasters.
- Produces candidate chip outputs and metadata.
- Uploads candidate artifacts to Hugging Face in batched commit operations.
- Uploads `candidate_status.csv` and `candidate_metadata.csv` together in one commit.

## Related documentation

- `REFINED_PIPELINE_SPEC.md`: authoritative requirements/spec checklist.
- `MOSAIC_SOURCE_QUALITY_RUNBOOK.md`: operational runbook for monthly scoring and fallback behavior.
- `landslide_workflow.md`: legacy scientific workflow notes and broader methodology context.
