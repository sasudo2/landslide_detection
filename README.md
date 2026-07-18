# Landslide susceptibility detection

This project identifies landslide susceptibility using risk factors collected from
satellite images (Sentinel-2 optical + Sentinel-1 SAR via Google Earth Engine) for
reported landslide incidents from the Bipad portal.

## Workflow (two stages)

1. **`extracting_data.ipynb`** — for each incident (date + bounding box) download a
   **single-date, same-day spatial mosaic** of the exact MGRS tiles overlapping the AOI,
   on the pre-event and post-event side of the incident date. This avoids the
   "cartoonish" artifact of the old 18-month temporal median composite. Outputs per
   incident (under `incident_<ID>/`):
   - `incident_<ID>_before.tif` — 13 bands: B1..B12 + SCL, single best pre-event date
   - `incident_<ID>_after.tif`  — same, single best post-event date
   - `incident_<ID>_slope.tif` — SRTM slope (30 m)
   - `incident_<ID>_aspect.tif` — SRTM aspect (30 m)
    - `incident_<ID>_sar_pre.tif`  — Sentinel-1 GRD VV/VH, pre-event (optional, same orbit pass)
    - `incident_<ID>_sar_post.tif` — Sentinel-1 GRD VV/VH, post-event (optional, same orbit pass)
2. **`candidate_detection.ipynb`** — narrows each incident zone to a small candidate
   mask by fusing optical change indices (ΔNDVI, ΔNDWI, ΔBSI, ΔNBR) masked by slope,
   then extracts connected-component blobs. For each candidate it clips the source
   rasters (before/after/S2, slope, aspect, and paired SAR when present) to the
   buffered bbox and writes them to `clipped_images/incident_<ID>/candidate_<N>/`.
   Uploads both:
   - `candidates/incident_<ID>_candidates.json` — ROI metadata (bbox, area, elongation).
   - `clipped_images/incident_<ID>/` —    per-candidate clipped image chips (before/after/slope/aspect; SAR NOT clipped).

All files are uploaded to the Hugging Face dataset `sasudo2/landslides` preserving the
`incident_<ID>/`, `candidates/`, and `clipped_images/` folder structure.

See `landslide_workflow.md` for the full scientific rationale (stage-by-stage why/how,
references, and the SAR + DEM fusion design).

## Work done
- Collection of all reported landslide cases from Bipad portal.
- Pre/post single-date mosaics + DEM derivatives downloaded for the incident set.

## Work under progress
- Before/after candidate narrowing using multi-index change fusion (Stage 1 of the workflow).
- Adding the Sentinel-1 SAR change cue and BBUnet / APSAM refinement (Stage 2).
