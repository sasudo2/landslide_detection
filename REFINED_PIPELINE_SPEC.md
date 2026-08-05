# Nepal Landslide Pipeline Refined Specification

This document is the authoritative spec for the current notebook pipeline in this repository.

## Pipeline notebooks

1. `incident_download.ipynb`
2. `candidate_detection.ipynb`

## Global rules

1. Input CSV path:
   `/kaggle/input/datasets/sanjayashrestha123/landslide-reproted/landslides_from_2018_to_2026.csv`
2. Required CSV columns:
   `id,title,incident_on,min_lon,min_lat,max_lon,max_lat`
3. Incident filtering is row index based (`START_IDX`, `END_IDX`).
4. Planet auth source is environment variable `PL_API_KEY`.
5. Incident download is monthly-first with daily fallback, no legacy toggle branch.
6. All incident runs must be rerun-safe via local manifest and per-incident status.

## Notebook 1: incident_download.ipynb

### Required behavior

1. Read selected incident rows and clamp AOI extent to a configured maximum degree span.
2. Use incident-centered directional month search:
   - before side evaluates only months before the incident month,
   - after side evaluates incident month and later,
   - search span is controlled by `MONTH_RADIUS` (default 6).
3. For each side (before/after), evaluate monthly candidate mosaics in the side-appropriate incident-centered window.
4. Monthly mosaic discovery must try both naming patterns:
   - `global_monthly_{year}_{month:02d}_mosaic`
   - `ps_monthly_analytic_sr_subscription_{year}_{month:02d}_mosaic`
5. For each monthly candidate mosaic:
   - perform metadata screen to reject visual-like mosaics (`datatype` byte/uint8 or `< 4` bands when specified),
   - query AOI-intersecting quads,
   - apply cloud gate from quad metadata (`MOSAIC_MAX_AOI_CLOUD`, `STRICT_MOSAIC_CLOUD_CHECK`),
   - download quad GeoTIFFs,
   - merge quads,
   - clip to AOI via polygon masking,
   - verify real output file has at least 4 bands.
6. Score valid monthly candidates using quality metrics:
   - valid pixel fraction,
   - NIR sharpness (Laplacian variance-derived),
   - contrast,
   - haze proxy.
7. Select nearest valid monthly candidate first, then higher score as tie-breaker.
8. If no monthly candidate passes `MIN_MONTHLY_QUALITY`, trigger daily fallback:
   - run Data API quick-search for `PSScene` around an incident-proximal, side-aware date window,
   - create Orders API request using `analytic_udm2` and AOI clip,
   - poll order state with retries/timeouts,
   - download clipped SR and UDM2 outputs,
   - reject fallback scene if UDM2 cloud exceeds `DAILY_MAX_UDM2_CLOUD_PCT`,
   - verify SR output has >=4 bands before accepting.
9. Save side outputs as:
   - `incident_{ID}_before.tif`
   - `incident_{ID}_after.tif`
10. Save per-incident quality report:
    - `incident_{ID}_quality_report.json`
11. Attempt GEE support products (when EE init succeeds):
    - `incident_{ID}_gee_before.tif` (if available),
    - `incident_{ID}_sar_pre.tif`,
    - `incident_{ID}_sar_post.tif`,
    - `incident_{ID}_slope.tif`,
    - `incident_{ID}_gee_aspect.tif`.
12. Update and persist manifest after each incident.
13. Persist run-level outputs:
    - `run_summary.csv`
    - `final_summary.json`

### Required run-level accounting

`final_summary.json` must include:
- total incidents processed,
- monthly success count,
- daily fallback success count,
- failure count,
- aggregated failure reasons.

### Reliability requirements

1. HTTP requests use retry/backoff for transient server/rate-limit failures.
2. Partial file writes use temporary files and atomic replace.
3. Per-incident failures are isolated and do not crash the whole batch.
4. Manifest-based skipping must prevent reprocessing incidents already marked successful.

## Notebook 2: candidate_detection.ipynb

### Required behavior

1. Read incident rasters from the generated incident folders.
2. Run change-based candidate extraction logic.
3. Emit candidate chip folders and per-candidate metadata.
4. Upload candidate artifacts to Hugging Face using batched commit operations.
5. Upload `candidate_status.csv` and `candidate_metadata.csv` together in one commit.

## Naming specification

Per incident folder:
- `incident_{ID}_before.tif`
- `incident_{ID}_after.tif`
- `incident_{ID}_quality_report.json`
- `incident_{ID}_gee_before.tif` (if available)
- `incident_{ID}_sar_pre.tif` (if available)
- `incident_{ID}_sar_post.tif` (if available)
- `incident_{ID}_slope.tif` (if available)
- `incident_{ID}_gee_aspect.tif` (if available)

Run-level files:
- `run_manifest.json`
- `run_summary.csv`
- `final_summary.json`

## Acceptance checklist

1. Index-range filtering works (`START_IDX`, `END_IDX`).
2. Monthly discovery checks both naming patterns in an incident-centered directional window.
3. Monthly candidates are screened by metadata and by real-file band count.
4. Cloud gate is enforced from quad metadata with strict/non-strict behavior.
5. Quality scoring is computed and best monthly candidate is selected by score.
6. Daily fallback is triggered when monthly quality is insufficient.
7. Daily fallback enforces UDM2 cloud threshold and >=4-band SR verification.
8. Incident quality JSON is written for every incident (success or failure).
9. Manifest skip behavior makes reruns idempotent for successful incidents.
10. Final summary includes monthly vs fallback success accounting and failure reasons.
11. Candidate notebook uploads chip outputs in batch commits.
12. Candidate summary CSVs are uploaded in a single commit.
