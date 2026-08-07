# Planet Quarterly Composite Selection Runbook

This runbook documents the current Planet path in `incident_download.ipynb`: pulling before/after optical imagery from **precomputed quarterly Planet mosaic composites** via the Mosaics/quads API.

## Scope

Applies to quarterly composite discovery, quad download/clip, and the GEE support-layer fallback. There is no per-scene order creation or order log in this flow.

## End-to-end selection flow

For each incident:
1. Clamp the AOI extent to `MAX_AOI_DEG`.
2. Select the after mosaic:
   - anchor at the incident date's quarter,
   - scan forward up to `MOSAIC_FORWARD_PERIODS` quarters for the first published mosaic,
   - query by name with `MOSAIC_NAME_TEMPLATE`.
3. Select the before mosaic:
   - start 1 quarter before the incident quarter (`MOSAIC_BACKWARD_START`),
   - scan back up to `MOSAIC_BACKWARD_PERIODS` quarters for the first published mosaic.
4. For the selected mosaic (if any):
   - query AOI-intersecting quads via the bbox,
   - download each quad GeoTIFF,
   - merge quads if more than one,
   - window-clip to the exact incident bbox (transforming the bbox to the mosaic CRS, typically EPSG:3857).
5. Save outputs:
   - `incident_{ID}_after.tif`
   - `incident_{ID}_planet_before.tif`
6. Download GEE support layers (when EE initializes):
   - `incident_{ID}_gee_before.tif` (Sentinel-2 before fallback, if a cloud-free scene exists),
   - `incident_{ID}_sar_pre.tif`, `incident_{ID}_sar_post.tif` (Sentinel-1 VV,VH),
   - `incident_{ID}_slope.tif`, `incident_{ID}_gee_aspect.tif` (SRTM).
7. Upload every downloaded TIFF to `raw_images/raw_incidents/incident_{ID}/` on Hugging Face and append a row to `download_log.csv`.

## Critical configuration

- `USE_MOSAIC_COMPOSITES`: must be `True` (this is the primary path; the per-scene Orders branch is legacy).
- `MOSAIC_CADENCE`: `'quarterly'`.
- `MOSAIC_NAME_TEMPLATE`: name pattern for discovery (default `global_quarterly_{year}q{quarter}_mosaic`). Must match what the Planet account actually publishes.
- `MOSAIC_FORWARD_PERIODS`: after-side scan width (quarters).
- `MOSAIC_BACKWARD_START` / `MOSAIC_BACKWARD_PERIODS`: before-side scan start and width (quarters).
- `PRE_DAYS` / `POST_DAYS`: GEE Sentinel-2/SAR temporal windows around the incident date.
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

- `No published after-mosaic found ...` (no mosaic published in the forward window)
- `No published before-mosaic found ...` (no mosaic published in the backward window)
- `No quads found covering ...` (mosaic exists but has no data over the AOI)
- `AOI does not intersect mosaic quad data` (empty window after bbox transform)
- GEE download failures for support layers (logged per-asset, do not abort the run)

## Tuning guidance

If too many incidents find no mosaic:
1. Increase `MOSAIC_FORWARD_PERIODS` (after) or `MOSAIC_BACKWARD_PERIODS` (before).
2. Verify `MOSAIC_NAME_TEMPLATE` matches the account's published quarterly series (the notebook prints sample series names on startup).
3. Confirm `MOSAIC_CADENCE` matches the series interval actually available.

## Validation checklist for production runs

1. Confirm `PL_API_KEY` and HF token secrets are set.
2. Dry-run with a small row range (`START_IDX`, `END_IDX`).
3. Verify the printed sample mosaic series names match `MOSAIC_NAME_TEMPLATE`.
4. Inspect several `incident_{ID}_after.tif` / `incident_{ID}_planet_before.tif` outputs for the expected AOI extent.
5. Confirm the candidate notebook consumes generated before/after/support files successfully.