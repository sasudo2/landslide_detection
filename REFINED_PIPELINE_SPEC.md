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
4. Cloud threshold for before and after imagery: `< 5%`.
5. Before window: up to 6 months before incident date.
6. Post window: up to 1 month after incident date for ancillary GEE/SAR data
   (`incident_download.ipynb`). Planet's own after-image search
   (`planet_order_creation.ipynb`) widens this to 2 months (`POST_DAYS=60`) since a
   1-month window too often has no <5%-cloud PlanetScope scene, especially during/after
   monsoon cloud cover.
7. Date prioritization: closest date first, then least cloud.
8. Hugging Face dataset: `sasudo2/landslides`.
9. Storage layout:
   - `raw_images/raw_incidents/incident_{ID}/` for raw incident rasters
   - `raw_images/order_log.csv` for order tracking and dedup
   - `candidates/` for candidate outputs

## Notebook 1: `planet_order_creation.ipynb`

### Required behavior

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

1. Read Hugging Face files and Planet order state; only process missing artifacts.
2. Download Planet after imagery from preordered incidents.
3. If multiple assets/scenes exist for an incident, mosaic to one:
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
6. Produce optical and SAR chi-square/confidence maps separately, then fuse.
7. Apply slope masking.
8. Run SLIC using feature stack:
   - fused confidence
   - normalized after-image texture channels
   - normalized slope
9. Use dual-threshold hysteresis on confidence probabilities.
10. Extract connected blobs, filter by area/elongation, rank severity from IR-MAD confidence/chi-square, keep top-N.
11. Save candidate chips/masks/metadata under `candidates/`.

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
2. Cloud threshold `< 5%` is enforced for before and after selection.
3. Before ordering default is OFF in Notebook 1.
4. Duplicate orders are prevented via `raw_images/order_log.csv`.
5. Multi-scene orders are mosaicked in Notebook 2.
6. Planet-before and GEE-before are both preserved when available.
7. GEE-before always exists if Planet-before is missing.
8. SAR pre/post VV+VH, slope, and aspect are generated for processed incidents.
9. Notebook 3 uses IR-MAD optical (4 bands) and IR-MAD SAR (VV/VH).
10. SCL is used only for masking, never as IR-MAD feature input.
11. Candidate outputs are written under `candidates/`.
12. Re-running pipeline is idempotent (skip behavior via HF logs/files).
