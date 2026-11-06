# Planet Quarterly Composite Selection Runbook

This runbook documents the current Planet path in `notebooks/pipeline/stage_1_data_acquisition/incident_download.ipynb`: pulling before/after optical imagery from **precomputed quarterly Planet mosaic composites** via the Mosaics/quads API.

## Scope

Applies to quarterly composite discovery, quad download/clip, and the GEE support-layer fallback. There is no per-scene order creation or order log in this flow.

## End-to-end selection flow

For each incident:
1. Clamp the AOI extent to `MAX_AOI_DEG`.
2. Select the after mosaic:
   - consider only dry-season quarters (all months in `DRY_SEASON_MONTHS`; default Q4 Oct–Dec and Q1 Jan–Mar),
   - walk forward from the quarter after the incident's quarter, trying up to `MOSAIC_MAX_DRY_PERIODS_FORWARD` dry quarters for the first published mosaic,
   - query by name with `MOSAIC_NAME_TEMPLATE`.
3. Select the before mosaic:
   - walk back from the quarter before the incident's quarter, trying up to `MOSAIC_MAX_DRY_PERIODS_BACK` dry quarters for the first published mosaic.
   - Example: a July 2024 incident uses 2024 Q1 (before) and 2024 Q4 (after); an October 2024 incident uses 2024 Q1 and 2025 Q1, since 2024 Q4 contains pre-event days.
4. For the selected mosaic (if any):
   - query AOI-intersecting quads via the bbox,
   - download each quad GeoTIFF,
   - merge quads if more than one,
   - window-clip to the exact incident bbox (transforming the bbox to the mosaic CRS, typically EPSG:3857).
5. Save outputs:
   - `incident_{ID}_after.tif`
   - `incident_{ID}_planet_before.tif`
6. Download GEE support layers (when EE initializes):
   - `incident_{ID}_gee_before.tif` (Sentinel-2 before fallback, if a cloud-free dry-season scene exists),
   - `incident_{ID}_sar_pre.tif`, `incident_{ID}_sar_post.tif` (Sentinel-1 VV,VH),
   - `incident_{ID}_slope.tif`, `incident_{ID}_gee_aspect.tif` (SRTM).
7. Upload every downloaded TIFF to `raw_images/raw_incidents/incident_{ID}/` on Hugging Face and append a row to `download_log.csv`.

## Critical configuration

- `USE_MOSAIC_COMPOSITES`: must be `True` (this is the primary path; the per-scene Orders branch is legacy).
- `MOSAIC_CADENCE`: `'quarterly'`.
- `MOSAIC_NAME_TEMPLATE`: name pattern for discovery (default `global_quarterly_{year}q{quarter}_mosaic`). Must match what the Planet account actually publishes.
- `DRY_SEASON_MONTHS`: months considered cloud-light in Nepal (default `(10, 11, 12, 1, 2, 3)`); a mosaic period qualifies only if all its months are listed.
- `MOSAIC_MAX_DRY_PERIODS_FORWARD`: number of dry-season periods tried on the after side.
- `MOSAIC_MAX_DRY_PERIODS_BACK`: number of dry-season periods tried on the before side.
- `S2_DRY_SEASON_LOOKBACK_DAYS`: how far back the GEE Sentinel-2 before fallback searches dry-season scenes.
- `PRE_DAYS` / `POST_DAYS`: GEE SAR temporal windows around the incident date.
- `CLOUD_MAX_AOI`: max AOI cloud fraction for the GEE Sentinel-2 before fallback.
- `MAX_AOI_DEG`: maximum degree span an AOI is clamped to.

## Artifacts written per incident

- `incident_{ID}_after.tif`
- `incident_{ID}_planet_before.tif`
- support layers when available:
  - `incident_{ID}_gee_before.tif`
  - `incident_{ID}_sar_pre.tif`
  - `incident_{ID}_sar_post.tif`
  - `incident_{ID}_slope.tif`
  - `incident_{ID}_gee_aspect.tif`

## Rerun behavior

- Incidents that already have every mandatory file on Hugging Face are skipped up front.
- `download_log.csv` is overwritten with the current run's per-incident records.

## Common failure reasons

- `No published dry-season after-mosaic found ...` (no dry-season mosaic published in the forward window, e.g. a recent incident whose next dry season has not been published yet)
- `No published dry-season before-mosaic found ...` (no dry-season mosaic published in the backward window)
- `No quads found covering ...` (mosaic exists but has no data over the AOI)
- `AOI does not intersect mosaic quad data` (empty window after bbox transform)
- GEE download failures for support layers (logged per-asset, do not abort the run)

## Tuning guidance

If too many incidents find no mosaic:
1. Increase `MOSAIC_MAX_DRY_PERIODS_FORWARD` (after) or `MOSAIC_MAX_DRY_PERIODS_BACK` (before).
2. Verify `MOSAIC_NAME_TEMPLATE` matches the account's published quarterly series (the notebook prints sample series names on startup).
3. Confirm `MOSAIC_CADENCE` matches the series interval actually available.

## Validation checklist for production runs

1. Confirm `PL_API_KEY` and HF token secrets are set.
2. Dry-run with a small row range (`START_IDX`, `END_IDX`).
3. Verify the printed sample mosaic series names match `MOSAIC_NAME_TEMPLATE`.
4. Inspect several `incident_{ID}_after.tif` / `incident_{ID}_planet_before.tif` outputs for the expected AOI extent.
5. Confirm the candidate notebook consumes generated before/after/support files successfully.