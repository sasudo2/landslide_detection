# Planet Mosaic Quality Runbook

This runbook explains why monthly Planet mosaic outputs can look softer than expected,
how this pipeline currently downloads imagery, and the exact checks required to ensure
we are using high-fidelity SR mosaics for landslide analysis.

## Scope

Applies to [incident_download.ipynb](incident_download.ipynb) when
USE_MOSAIC_COMPOSITES is enabled.

## Executive Summary

When a 4.77 m monthly mosaic appears worse than expected, the cause is usually one of:

1. Viewer/tile downsampling path instead of true GeoTIFF download.
2. Wrong product family (Visual RGB/8-bit vs SR/4-band/16-bit).
3. SR normalization/seam-blending tradeoffs in composited products.
4. Month/region source-scene scarcity (monsoon cloud, steep terrain, low clear-scene density).

In this repository's current notebook path, downloads come from the Basemaps API quads
endpoint and are clipped locally, so issue #1 is usually ruled out.

## Current Download Path (What The Notebook Actually Does)

With USE_MOSAIC_COMPOSITES=True, the notebook:

1. Finds before/after monthly mosaics.
2. Calls the Basemaps quads endpoint for AOI coverage.
3. Downloads quad GeoTIFFs from each quad's _links.download URL.
4. Mosaics and clips to incident AOI.

This is native raster download flow, not WMTS/XYZ screenshot flow.

## Quality Policy Implemented In Notebook

The notebook now enforces strict SR quality checks by default:

- ALLOW_VISUAL_MOSAIC_FALLBACK defaults to False.
- Mosaic metadata is re-fetched from per-mosaic detail endpoint before quality checks.
- Non-SR candidates are rejected unless fallback is explicitly enabled.
- Diagnostic logs print:
  - selected template
  - fallback policy
  - datatype, bands, product_type

Additional hardening (2026-08-05):

- Fail-closed metadata policy:
   - STRICT_MOSAIC_METADATA_CHECK=True
   - Missing/ambiguous metadata is treated as unverifiable and rejected by default.
- Reduced string-matching fragility:
   - strong pass is primarily numeric (uint16 + band_count>=4), not naming keywords.
   - ALLOW_SR_KEYWORD_FALLBACK=False by default (optional compatibility mode only).
- Post-download validation:
   - every optical output is opened with rasterio and must satisfy
      dtype==uint16 and band_count>=4, or it is removed and logged as rejected.
- Explicit incident issue tracking:
   - quality/availability failures are appended to per-incident issues and persisted
      in download_log.csv, preventing silent coverage gaps.
- Mixed-fidelity protection:
   - uploaded status becomes uploaded_with_warnings when issues exist, with details
      in error text to make any schema inconsistency visible.

### SR acceptance heuristics

A mosaic is accepted when it appears SR-like based on metadata:

- Positive signals:
  - product_type includes analytic/surface_reflectance/sr, or
  - datatype looks uint16, and
  - bands is unknown or >= 4.
- Negative signals:
  - datatype is byte/uint8, or
  - bands is present and < 4.

If negative signals are present, the mosaic is rejected unless
ALLOW_VISUAL_MOSAIC_FALLBACK=True.

## Why This Matters For Notebook 3

Candidate detection uses four optical channels and NDVI-driven evidence. Visual
products (typically 8-bit RGB+alpha) degrade or invalidate NIR-dependent workflows.
Strict SR gating prevents silent quality regressions in downstream IR-MAD/NDVI fusion.

## Configuration Checklist Before A Production Run

1. Keep USE_MOSAIC_COMPOSITES aligned between notebook 1 and notebook 2.
2. Verify MOSAIC_NAME_TEMPLATE resolves to SR products on the current Planet plan.
3. Keep ALLOW_VISUAL_MOSAIC_FALLBACK=False for analysis-quality runs.
4. Run one-incident smoke test before full batch.

## One-Incident Validation Procedure

1. In notebook configuration:
   - START_IDX and END_IDX to isolate one known incident.
2. Run auth/init cell and inspect logs:
   - sample monthly mosaic names
   - per-mosaic datatype/bands/product_type
   - template and fallback policy print
3. Run download cell for that incident.
4. Verify outputs contain expected optical files and no SR-rejection surprises.

## Interpreting Log Messages

Expected good path:

- Planet before/after ready with selected mosaic name.
- No rejection warning for non-SR mosaic.

Expected protective rejection path:

- Rejecting non-SR mosaic '<name>' (datatype=..., bands=..., product_type=...)
- Set ALLOW_VISUAL_MOSAIC_FALLBACK=True only if you explicitly accept lower-fidelity Visual mosaics.

This rejection is intentional and prevents low-quality/unsupported optical inputs from
silently entering the training/detection flow.

Additional explicit issue examples now persisted in download_log.csv:

- after_mosaic_missing_or_rejected_quality (...)
- before_mosaic_missing_or_rejected_quality (...)
- incident_<id>_after: dtype=... != required uint16
- incident_<id>_planet_before: band_count=... < required 4
- gee_before_no_cloud_free_scene

If a run partially succeeds (ancillary files uploaded but optical warnings exist), the
status is uploaded_with_warnings and the warning list is preserved in the error field.

## If SR Mosaics Are Unavailable On The Account

Options, in priority order:

1. Select a different monthly SR series available on the plan and update template.
2. Temporarily switch to per-scene mode with UDM2 masking:
   - USE_MOSAIC_COMPOSITES=False
3. As last resort, set ALLOW_VISUAL_MOSAIC_FALLBACK=True for exploratory visualization
   only, not for quantitative analysis.

## Known Tradeoff Notes

Even correct SR monthly mosaics can still look softer than native daily scenes due to
compositing, normalization, and seam handling. This is expected. For highest per-scene
sharpness in difficult months, per-scene download mode may outperform monthly mosaics at
the cost of more operational complexity.

## Related Files

- [incident_download.ipynb](incident_download.ipynb)
- [README.md](README.md)
- [REFINED_PIPELINE_SPEC.md](REFINED_PIPELINE_SPEC.md)
