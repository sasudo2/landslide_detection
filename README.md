# Landslide susceptibility detection

This repository processes reported landslide incidents into standardized raster inputs and candidate chips for downstream review.

The active processing flow is implemented in:
1. `incident_download.ipynb` (Planet quarterly composite download + GEE support layers)
2. `candidate_detection.ipynb` (change-based candidate extraction and candidate uploads)

`planet_order_creation.ipynb` is not part of the current flow and can be ignored.

Hugging Face dataset repo `sasudo2/landslides` is the storage backend.

## Current status

The incident download logic uses precomputed **Planet quarterly mosaic composites** as the primary optical source:
- before/after optical imagery is fetched directly from Planet's Mosaics/quads API per incident (no per-scene order submission or polling),
- Planet's own compositing already removes cloud/shadow and normalizes color across scenes,
- no per-scene order creation, order log, per-incident quality-report, or run manifest is used anymore.

## Notebook 1: incident_download.ipynb

Purpose:
- For each incident AOI, produce before and after optical rasters plus support layers.

Key behavior:
1. Read incident rows from CSV and clamp AOI extent to bounded size (`MAX_AOI_DEG`).
2. For each side (before/after), select a precomputed quarterly Planet mosaic from Nepal's **dry season** (`DRY_SEASON_MONTHS`, default Oct–Mar, i.e. Q4 and Q1) to avoid monsoon cloud cover:
   - after side: the nearest published dry-season quarter strictly after the incident's quarter, trying up to `MOSAIC_MAX_DRY_PERIODS_FORWARD` dry quarters,
   - before side: the nearest published dry-season quarter strictly before the incident's quarter, trying up to `MOSAIC_MAX_DRY_PERIODS_BACK` dry quarters,
   - the incident's own quarter is never used, so neither composite mixes pre- and post-event pixels,
   - mosaic discovery uses the naming pattern `MOSAIC_NAME_TEMPLATE` (`global_quarterly_{year}q{quarter}_mosaic`).
3. For each selected mosaic:
   - query quads intersecting the AOI (bbox),
   - download the quad GeoTIFFs,
   - merge them if there is more than one,
   - window-clip to the exact incident bbox.
4. Write the Planet outputs:
   - `incident_{ID}_after.tif`,
   - `incident_{ID}_planet_before.tif`.
5. Download GEE support layers when available:
   - optical before fallback (`incident_{ID}_gee_before.tif`: most recent cloud-free Sentinel-2 scene from dry-season months within `S2_DRY_SEASON_LOOKBACK_DAYS` before the incident),
   - SAR pre and post (`VV,VH`),
   - slope and aspect.
6. Upload all downloaded TIFFs to `raw_images/raw_incidents/incident_{ID}/` on Hugging Face in batched commits.
7. Upload a run-level `download_log.csv` summarizing per-incident outcomes.

### Authentication and environment

Required:
- `Lokesh_planet` secret holding the Planet API key (for the Mosaics/quads API)
- `huggingface_token` secret for Hugging Face uploads

Optional (for GEE service-account init):
- `GEE_PROJECT`
- `GEE_SERVICE_ACCOUNT`
- `GEE_KEY_PATH` (or `gee_json_key` secret)

## Output layout

Primary working outputs are generated under:
- `/kaggle/working/raw_incidents/`

Per incident:
- `incident_{ID}/incident_{ID}_after.tif`
- `incident_{ID}/incident_{ID}_planet_before.tif`
- `incident_{ID}/incident_{ID}_gee_before.tif` (when available)
- `incident_{ID}/incident_{ID}_sar_pre.tif` (when available)
- `incident_{ID}/incident_{ID}_sar_post.tif` (when available)
- `incident_{ID}/incident_{ID}_slope.tif` (when available)
- `incident_{ID}/incident_{ID}_gee_aspect.tif` (when available)

Run-level artifacts:
- `download_log.csv`

## Notebook 2: candidate_detection.ipynb

Purpose:
- Build ranked candidate chips per incident from multi-source raster evidence.

Current behavior highlights:
- Uses available before/after/support rasters (treating the optical pair as RGB-first since the quarterly composites are color/visual mosaics).
- Produces candidate chip outputs and metadata.
- Uploads candidate artifacts to Hugging Face in batched commit operations.
- Uploads `candidate_status.csv` and `candidate_metadata.csv` together in one commit.

## Related documentation

- `REFINED_PIPELINE_SPEC.md`: authoritative requirements/spec checklist.
- `MOSAIC_SOURCE_QUALITY_RUNBOOK.md`: operational runbook for quarterly composite selection.
- `landslide_workflow.md`: legacy scientific workflow notes and broader methodology context.

## Candidate-detection ablation study

The study runs as eight self-contained Kaggle notebooks. They contain their own pipeline code and do not import repository modules or require a runtime `.py` file.

| Notebook | Role |
|---|---|
| `ablation_baseline.ipynb` | Optical + SAR, slope mask/feature, SLIC, Bonferroni |
| `ablation_optical_only.ipynb` | Optical only |
| `ablation_sar_only.ipynb` | SAR only |
| `ablation_no_slope.ipynb` | No slope mask and no slope segmentation feature |
| `ablation_no_slic.ipynb` | No SLIC; pixelwise evidence and significance gate |
| `ablation_no_bonferroni.ipynb` | Uncorrected `alpha=0.05`; same normal-approximation gate |
| `ablation_compare.ipynb` | Read-only validation, then writes `comparison.csv` |
| `ablation_generate_notebooks.ipynb` | Sole editable source for the six run notebooks |

The six run notebooks are generated files. Edit `SHARED_CONFIG` or `VARIANTS` in
`ablation_generate_notebooks.ipynb`, run it, and commit the regenerated notebooks. Do not hand-edit
a generated run notebook.

### Fixed cohort

Every run notebook reads the attached `landslide-reproted` dataset file
`landslides_from_2018_to_2026.csv`, selects exactly `df.iloc[900:1000]`, preserves the CSV row
order, and uses its `id` column as the 100 incident IDs.

Before any computation, each notebook resolves the configured raw commit SHA, lists the raw files
at that immutable commit, and verifies that every selected incident has:

- `after.tif`
- `slope.tif`
- `sar_pre.tif`
- `sar_post.tif`
- either `planet_before.tif` or `gee_before.tif`

If any selected incident is incomplete, the notebook fails before computing. The notebooks do not
enumerate, inspect, or skip production `candidates/` output; existing production candidates are
irrelevant to this study.

### Configure the shared study

In `ablation_generate_notebooks.ipynb`, edit `SHARED_CONFIG`:

```python
SHARED_CONFIG = {
    "raw_repo": "sasudo2/landslides",
    "raw_commit_sha": "REPLACE_WITH_40_CHARACTER_RAW_COMMIT_SHA",  # full 40-char SHA, never main
    "output_repo": "sasudo2/landslides",
    "output_revision": "main",
    "study_root": "ablations/study_01",
    "source_csv_name": "landslides_from_2018_to_2026.csv",
    "cohort_start": 900,
    "cohort_stop": 1000,
    "workers": 4,
}
```

Run the generator to rewrite all six run notebooks. Branches such as `main` are rejected for the raw
input: all six notebooks must use the same full immutable commit SHA and the same `STUDY_ROOT`.
To check for drift without rewriting, set `CHECK_ONLY = True` and run the generator; it regenerates
the notebooks in memory and compares every committed notebook against the canonical output.

The output repository and branch must already exist. Run notebooks scope Hub listings to the raw
input prefix and their own `<STUDY_ROOT>`; they never enumerate production `candidates/` files.

Each Kaggle run notebook needs:

1. **Settings -> Internet: On**
2. The `landslide-reproted` dataset attached, containing `landslides_from_2018_to_2026.csv`
3. The Kaggle secret `huggingface_token`, with read/write access to `output_repo`
4. The setup cell executed before processing

The generated notebooks install their own dependencies (`numpy`, `pandas`, `scipy`, `rasterio`,
`scikit-image`, `huggingface_hub`). No repository checkout or shared Python module is required.

### Launch the six runs

Start all six run notebooks independently and in parallel. Each notebook:

1. Validates its own variant configuration and fixed cohort.
2. Resolves the raw commit and verifies all 100 incidents.
3. Reads sibling `<STUDY_ROOT>/<other-variant>/study.json` manifests when present.
4. Compares shared identity fields and refuses to run on any conflict.
5. Refuses to overwrite or resume its own existing variant folder.

Absent sibling manifests are allowed, so the six notebooks can start together. Each notebook writes
only one variant folder and never modifies a sibling folder, the study root itself, production
`candidates/`, or a repository-wide `.gitattributes`.

Hub calls are retried with bounded backoff for transient failures. Per-incident errors and failed
chip-upload batches are recorded as failures, never silently converted into zero-candidate results.

### Isolated remote output layout

```text
<STUDY_ROOT>/
  baseline/
    study.json
    run.json
    candidate_status.csv
    candidate_metadata.csv
    diagnostics.json
    summary.json
    incident_<id>/
      candidate_<n>/
        ... before, after, slope, mask chips ...
  optical_only/
  sar_only/
  no_slope/
  no_slic/
  no_bonferroni/
  comparison.csv               # comparison notebook only
```

`study.json` is the immutable compatibility contract. It records the schema version, variant
switches, raw repository and commit SHA, source CSV name and hash, `df.iloc[900:1000]` cohort
declaration, ordered 100 incident IDs, required raw-input policy, all numerical settings, installed
package versions, pipeline/template SHA, generated-notebook SHA, timestamp, and run environment.
`run.json` records execution-level details such as workers, start/finish times, status, and counts.

`candidate_status.csv` has one row for every incident, including zero-candidate and failed
incidents. `candidate_metadata.csv` is sorted and keeps the notebook-compatible ranking and
bounding-box columns. `diagnostics.json` records per-incident test counts, critical z thresholds,
mask pixels, and candidate counts. `summary.json` records successful/failed counts and candidate
counts.

### Rerun behavior

If a run is interrupted or fails, do not resume its variant folder. Choose a **new** `STUDY_ROOT`,
regenerate the six notebooks from the generator, and launch all six again. A variant folder is
reserved before processing, and an existing folder for the same variant is always refused.

### Comparison notebook

Run `ablation_compare.ipynb` only after all six run notebooks have finished. It is read-only until
every validation succeeds:

1. All six variant folders exist with the expected files.
2. The shared study-identity fields are equivalent across all six `study.json` files.
3. Every variant declares its expected ablation switches.
4. Every study has the same ordered 100 incident IDs.
5. Every run is complete and has no failed incidents.

Only then does it write `<STUDY_ROOT>/comparison.csv`, with one row per variant: cohort size,
successful and failed counts, incidents with candidates, total candidates, candidates per successful
incident, shared raw commit, pipeline/template SHA, identity-check result, completion status, and
switches. Set `ALLOW_PARTIAL = True` only when a partial diagnostic report is explicitly wanted;
otherwise any failed or incomplete run blocks the comparison.

### Controls and interpretation

- These are six one-component-at-a-time runs, not a full factorial study.
- Baseline carries the `candidate_detection.ipynb` behavior: RGB-first optical IR-MAD, SAR VV/VH
  IR-MAD, geometric-mean fusion, a 10 m EPSG:4326 grid, slope masking, SLIC segments, segment-level
  chi-square evidence with a normal-approximation gate, Bonferroni correction, percentile
  hysteresis, morphology, area/elongation filtering, ranking, and chip export.
- `optical_only` never fits or reads SAR for detection. `sar_only` never reads optical pixel values
  for detection or segmentation; optical rasters are still used for the analysis-grid reference and
  review chips. `no_slope` removes both the slope mask and the slope segmentation feature. `no_slic`
  replaces segments with pixelwise confidence and significance tests, with Bonferroni over the full
  grid pixel count. `no_bonferroni` retains the same normal-approximation z-test at `alpha=0.05`.
- Percentiles (99.7/93), slope cutoff (20 degrees), morphology, blob limits, ranking, chip padding,
  and IR-MAD settings retain the notebook defaults.
- Each selected modality must have more than 100 valid paired pixels. Otherwise the incident is
  recorded as failed rather than silently falling back to an available modality, so the six cohorts
  stay comparable. Do not compare a failed incident as a zero detection.
- These runs report candidate detection counts, not accuracy. Precision, recall, IoU, and scientific
  conclusions require independent ground-truth labels; candidate counts alone do not demonstrate
  detection quality.
