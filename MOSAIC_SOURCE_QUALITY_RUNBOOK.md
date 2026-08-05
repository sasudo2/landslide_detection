# Planet Mosaic Cloud-Gating Runbook

This runbook documents the current monthly Planet mosaic behavior in this repository.
It focuses on cloud-based selection and operational debugging.

## Scope

Applies to [incident_download.ipynb](incident_download.ipynb) when
`USE_MOSAIC_COMPOSITES=True`.

## Current Policy (As Implemented)

Monthly mosaic selection uses AOI quad cloud metadata checks only.

- `MOSAIC_MAX_AOI_CLOUD` controls max allowed AOI cloud percentage.
- `STRICT_MOSAIC_CLOUD_CHECK` controls behavior when quad cloud metadata is missing:
  - `True`: reject that mosaic month.
  - `False`: allow that mosaic month.
- The notebook does not enforce SR/analytic product-type checks.

## Download Path

With `USE_MOSAIC_COMPOSITES=True`, the notebook:

1. Finds after/before monthly mosaics by name template.
2. Queries quad coverage for each incident AOI.
3. Evaluates AOI cloud from quad metadata.
4. Selects the first month in range that passes the cloud gate.
5. Downloads quad GeoTIFFs, mosaics if needed, clips to AOI.

This is native raster download via Basemaps quads API links, not tile/screenshot flow.

## Common Log Messages

Accepted month:

- `Selected after-mosaic ... (AOI cloud X.XX% across N quad(s))`
- `Selected before-mosaic ... (AOI cloud X.XX% across N quad(s))`

Rejected month due to cloud value:

- `Rejected after-mosaic ... AOI cloud X.XX% > Y.YY%`
- `Rejected before-mosaic ... AOI cloud X.XX% > Y.YY%`

Rejected month due to missing metadata in strict mode:

- `Rejected after-mosaic ... cloud metadata unavailable for AOI quads`
- `Rejected before-mosaic ... cloud metadata unavailable for AOI quads`

No month selected in search window:

- `No after-mosaic passed AOI cloud <= ... within ... month(s) forward - skipping`
- `No before-mosaic passed AOI cloud <= ... within ... month(s) back - skipping`

## Why Repeated Rejection Lines Appear

Repeated lines for the same month can happen when multiple incidents are processed in
parallel (`MAX_WORKERS > 1`) and each incident evaluates the same candidate month.

## Configuration Checklist Before Batch Runs

1. Confirm `MOSAIC_NAME_TEMPLATE` matches available account series names.
2. Decide strictness:
   - keep `STRICT_MOSAIC_CLOUD_CHECK=True` for fail-closed behavior,
   - set to `False` if missing metadata should not block downloads.
3. Set `MOSAIC_MAX_AOI_CLOUD` to your tolerance (lower is stricter).
4. Validate on one incident (`START_IDX`/`END_IDX`) before full batch.

## Operational Guidance

If too many incidents are skipped in composite mode:

1. Increase month search windows (`MOSAIC_FORWARD_MONTHS`, `MOSAIC_BACKWARD_MONTHS`).
2. Relax `MOSAIC_MAX_AOI_CLOUD` if your use case permits.
3. Set `STRICT_MOSAIC_CLOUD_CHECK=False` if metadata sparsity is common.
4. Switch to `USE_MOSAIC_COMPOSITES=False` to use legacy per-scene path with UDM2 masking.

## Related Files

- [incident_download.ipynb](incident_download.ipynb)
- [README.md](README.md)
- [REFINED_PIPELINE_SPEC.md](REFINED_PIPELINE_SPEC.md)
