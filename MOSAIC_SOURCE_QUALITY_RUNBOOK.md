# Planet Monthly Selection and Fallback Runbook

This runbook documents the current Planet path in `incident_download.ipynb`.

## Scope

Applies to monthly selection, quality scoring, and daily fallback behavior.

## End-to-end selection flow

For each incident side (before/after):
1. Build incident-centered monthly search space.
2. Enumerate month candidates directionally by side:
  - before: months before incident month,
  - after: incident month and later,
  - width controlled by `MONTH_RADIUS`.
3. Query mosaics by both naming patterns.
4. Reject visual-like metadata candidates.
5. Query quads over AOI and apply cloud gate.
6. Download and merge quads, clip to AOI.
7. Verify file has >=4 bands.
8. Compute quality score and keep nearest valid candidate (higher score as tie-breaker).
9. If best score is below floor, run daily fallback through quick-search + Orders API.
10. In fallback, reject scene if UDM2 cloud percent is above threshold.

## Critical configuration

- `MONTH_RADIUS`: monthly search width around incident month (default 6).
- `BEFORE_OFFSET_MONTHS`, `AFTER_OFFSET_MONTHS`: kept at 0 for incident-centered behavior.
- `MOSAIC_MAX_AOI_CLOUD`: max average AOI cloud percent accepted from quad metadata.
- `STRICT_MOSAIC_CLOUD_CHECK`:
  - `True`: reject candidate if cloud metadata missing.
  - `False`: allow candidate when cloud metadata missing.
- `MIN_MONTHLY_QUALITY`: minimum acceptable monthly score.
- `DAILY_SEARCH_WINDOW_DAYS`: daily fallback search span around target date.
- `DAILY_SCENE_CLOUD_MAX`: quick-search scene cloud filter.
- `DAILY_MAX_UDM2_CLOUD_PCT`: strict acceptance threshold after UDM2 evaluation.

## Monthly quality score composition

The quality score is a weighted combination of:
- valid coverage fraction (weight 0.35),
- NIR sharpness from Laplacian-variance proxy (weight 0.30),
- contrast metric (weight 0.25),
- inverse haze fraction heuristic (weight 0.10).

Score range is `[0,1]`. Candidates below `MIN_MONTHLY_QUALITY` do not pass monthly selection.

## Artifacts written per incident

- `incident_{ID}_before.tif`
- `incident_{ID}_after.tif`
- `incident_{ID}_quality_report.json`
- support layers when available:
  - `incident_{ID}_gee_before.tif`
  - `incident_{ID}_sar_pre.tif`
  - `incident_{ID}_sar_post.tif`
  - `incident_{ID}_slope.tif`
  - `incident_{ID}_gee_aspect.tif`

The quality report includes:
- evaluated monthly candidates and reject reasons,
- selected mode (`monthly_mosaic` or `daily_fallback`) per side,
- selected candidate details and score,
- fallback trigger reason,
- incident-level status and error (if failed).

## Rerun behavior

- `run_manifest.json` is updated after each incident.
- Incidents already marked success are skipped on rerun.
- Batch-level outputs are collected into `run_summary.csv` and `final_summary.json`.

## Common failure reasons

- `no_valid_monthly_candidate`
- `best_monthly_below_quality_floor:*`
- `No daily PSScene found in fallback search window`
- `Daily fallback rejected: UDM2 cloud ...`
- `Daily fallback SR has ... bands (<4)`
- order timeout or API failures

## Tuning guidance

If too many incidents fail monthly path:
1. Increase `MONTH_RADIUS`.
2. Relax `MOSAIC_MAX_AOI_CLOUD`.
3. Lower `MIN_MONTHLY_QUALITY` modestly.

If too many incidents fail fallback path:
1. Increase `DAILY_SEARCH_WINDOW_DAYS`.
2. Relax `DAILY_SCENE_CLOUD_MAX` modestly.
3. Relax `DAILY_MAX_UDM2_CLOUD_PCT` only if acceptable for downstream quality.

## Validation checklist for production runs

1. Confirm `PL_API_KEY` is set.
2. Dry-run with a small row range (`START_IDX`, `END_IDX`).
3. Inspect several `incident_{ID}_quality_report.json` files.
4. Confirm final summary counts align with per-incident outcomes.
5. Confirm candidate notebook consumes generated before/after/support files successfully.
