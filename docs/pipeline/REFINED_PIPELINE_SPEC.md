# Nepal Landslide Pipeline Refined Specification

This document is the authoritative spec for the current notebook pipeline in this repository.

## Pipeline notebooks

1. `notebooks/pipeline/stage_1_data_acquisition/incident_download.ipynb`
2. `notebooks/pipeline/stage_2_candidate_collection/` (candidate detection, candidate download, candidate deduplication)
3. `notebooks/pipeline/stage_3_annotation_and_png_export/` (annotation deduplication, cropped-image download, PNG export)

`notebooks/legacy/planet_order_creation.ipynb` is not part of the current pipeline and can be ignored.

## Global rules

1. Input CSV path:
   `/kaggle/input/datasets/sanjayashrestha123/landslide-reproted/landslides_from_2018_to_2026.csv`
2. Required CSV columns:
   `id,title,incident_on,min_lon,min_lat,max_lon,max_lat`
3. Incident filtering is row index based (`START_IDX`, `END_IDX`).
4. Planet auth source is environment variable `PL_API_KEY` (used against the Mosaics/quads API).
5. Optical before/after imagery is sourced from **precomputed quarterly Planet mosaic composites** via the Mosaics/quads API. No per-scene order creation, order log, or order polling is used.
6. All incident uploads go to Hugging Face under `raw_images/raw_incidents/incident_{ID}/` (rerun-safe via pre-upload existence checks).

## Stage 1: `notebooks/pipeline/stage_1_data_acquisition/incident_download.ipynb`

### Required behavior

1. Read selected incident rows and clamp AOI extent to a configured maximum degree span (`MAX_AOI_DEG`).
2. Use `USE_MOSAIC_COMPOSITES = True` (the primary path) with `MOSAIC_CADENCE = 'quarterly'`.
3. Restrict optical mosaics to Nepal's dry season: a period qualifies only if all its months are in `DRY_SEASON_MONTHS` (default `(10, 11, 12, 1, 2, 3)`, so only Q4 and Q1 for quarterly cadence).
4. Select the after mosaic: the nearest published dry-season period strictly after the incident's period (up to `MOSAIC_MAX_DRY_PERIODS_FORWARD` tried). Select the before mosaic: the nearest published dry-season period strictly before the incident's period (up to `MOSAIC_MAX_DRY_PERIODS_BACK` tried). The incident's own period is never used.
5. Mosaic discovery uses the name pattern `MOSAIC_NAME_TEMPLATE` (`global_quarterly_{year}q{quarter}_mosaic`). On startup the notebook prints sample series names and their datatype/bands so the template can be corrected if it resolves to the wrong (e.g. 8-bit Visual) product.
6. For each selected mosaic:
   - query AOI-intersecting quads via a bbox query,
   - download each quad GeoTIFF,
   - merge quads if there is more than one,
   - window-clip to the exact incident bbox (transforming the bbox into the mosaic CRS, typically EPSG:3857),
   - write `incident_{ID}_after.tif` and `incident_{ID}_planet_before.tif`.
7. Download GEE support layers (only when EE initializes), each in its own try/except:
   - `incident_{ID}_gee_before.tif` (Sentinel-2 before fallback: most recent scene from `DRY_SEASON_MONTHS` within `S2_DRY_SEASON_LOOKBACK_DAYS` before the incident with AOI cloud ≤ `CLOUD_MAX_AOI`),
   - SAR pre/post use `PRE_DAYS` / `POST_DAYS` windows (SAR is cloud-independent, so no dry-season restriction),
   - `incident_{ID}_sar_pre.tif`, `incident_{ID}_sar_post.tif` (Sentinel-1 VV,VH),
   - `incident_{ID}_slope.tif`, `incident_{ID}_gee_aspect.tif` (SRTM-based).
8. Upload every downloaded TIFF to Hugging Face in batched commits (`UPLOAD_BATCH_SIZE`), skipping incidents whose mandatory files already exist on HF.
9. Write run-level accounting to `download_log.csv` (per-incident status/error counts).

### Reliability requirements

1. HTTP requests use retry/backoff for transient server/rate-limit failures.
2. Partial downloads use temporary files and atomic replace.
3. Per-asset/per-incident failures are isolated and do not crash the whole batch.
4. Upload batches carefully (Hugging Face `create_commit` with `CommitOperationAdd`).

## Stage 2: Candidate collection

Run these in order:
1. `notebooks/pipeline/stage_2_candidate_collection/candidate_detection.ipynb`
2. `notebooks/pipeline/stage_2_candidate_collection/download_candidates.sh`
3. `notebooks/pipeline/stage_2_candidate_collection/deduplicate_candidates.py`

### Required behavior

1. Discover incidents that have complete raw imagery on Hugging Face (`after.tif`, `planet_before.tif` or `gee_before.tif`, `slope.tif`, `sar_pre.tif`, `sar_post.tif`).
2. Skip incidents that already have candidate outputs on HF (idempotent re-runs).
3. Reproject optical, SAR, and slope onto one common grid (`EPSG:4326`, ~10 m).
4. Run change-based candidate extraction:
   - optical change confidence via IR-MAD on RGB channels (the quarterly composites are RGB/visual mosaics, so NDVI/NIR loss fusion is skipped),
   - SAR change confidence via IR-MAD on VV/VH,
   - slope mask (`MIN_SLOPE_DEG`) to constrain detection to steep terrain,
   - geometric-mean fusion of available modalities, multiplied by the slope mask.
5. Generate candidates via SLIC superpixels, per-segment significance test (Bonferroni-controlled), hysteresis thresholding, morphology/blob filters, and area×severity ranking.
6. Export per-candidate chips: `before`, `after` (at native source resolution), `slope`, `mask` (on the analysis grid).
7. Upload candidate artifacts to Hugging Face using batched commit operations.
8. Upload `candidate_status.csv` and `candidate_metadata.csv` together in one commit.

## Stage 3: Annotation and PNG export

Run these in order:
1. `notebooks/pipeline/stage_3_annotation_and_png_export/annotation_dedup_build.ipynb` to resize/deduplicate annotation rows and append new rows to the labeled CSVs.
2. `notebooks/pipeline/stage_3_annotation_and_png_export/landslide_download.ipynb` to read `landslide.csv`, check for existing `landslide/incident_{incident}_candidate_{candidate}_after.tif` crops, and skip those rows before downloading any source imagery. For each new row, fetch the raw `incident_{ID}_after.tif` from `raw_images/raw_incidents/`, crop it to the annotation bbox, and produce an individual cropped TIFF plus a PNG.

Upload each cropped TIFF individually under `landslide/incident_{incident}_candidate_{candidate}_after.tif`. Accumulate all PNGs generated in the current run into one `landslide_png/landslide_png_yyyymmdd_hhmmss.zip`. Commit the TIFF files and the session ZIP together. The notebook consumes only `landslide.csv` from the configured annotation path and requires the Hugging Face write token; it does not submit Planet orders or read an order log.

## Naming specification

Per incident folder:
- `incident_{ID}_after.tif`
- `incident_{ID}_planet_before.tif`
- `incident_{ID}_gee_before.tif` (if available)
- `incident_{ID}_sar_pre.tif` (if available)
- `incident_{ID}_sar_post.tif` (if available)
- `incident_{ID}_slope.tif` (if available)
- `incident_{ID}_gee_aspect.tif` (if available)

Run-level files:
- `download_log.csv`

Candidate outputs under `candidates/`:
- `candidates/incident_{ID}/candidate_{k}/...`
- `candidates/candidate_status.csv`
- `candidates/candidate_metadata.csv`

## Acceptance checklist

1. Index-range filtering works (`START_IDX`, `END_IDX`).
2. AOI extent is clamped to `MAX_AOI_DEG`.
3. `USE_MOSAIC_COMPOSITES` is `True` with `MOSAIC_CADENCE = 'quarterly'`.
4. After/before mosaics are the nearest published dry-season periods strictly after/before the incident's period.
5. Mosaic discovery uses `MOSAIC_NAME_TEMPLATE`, and the notebook reports the account's existing series to validate the template.
6. Quads are downloaded, merged, and window-clipped to the exact AOI bbox (CRS-aware).
7. `incident_{ID}_after.tif` and `incident_{ID}_planet_before.tif` are written.
8. GEE support layers are attempted independently and never abort the run on failure.
9. Per-incident uploads to HF are batched, and already-complete incidents are skipped on rerun.
10. `download_log.csv` records per-incident status.
11. Candidate notebook skips incidents lacking mandatory raw files and skips incident with existing candidates.
12. Candidate notebook uploads chip outputs in batch commits; summary CSVs uploaded in a single commit.