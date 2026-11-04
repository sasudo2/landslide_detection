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

The study runs as seven self-contained Kaggle notebooks: six independent run notebooks and one optional comparison notebook. Each run notebook contains its own pipeline code and configuration. No notebook imports repository modules, executes another notebook, or depends on a shared generated artifact.

| Notebook | Role |
|---|---|
| `ablation_baseline.ipynb` | Optical + SAR, slope mask/feature, SLIC, Bonferroni |
| `ablation_optical_only.ipynb` | Optical only |
| `ablation_sar_only.ipynb` | SAR only |
| `ablation_no_slope.ipynb` | No slope mask and no slope segmentation feature |
| `ablation_no_slic.ipynb` | No SLIC; pixelwise evidence and significance gate |
| `ablation_no_bonferroni.ipynb` | Uncorrected `alpha=0.05`; same normal-approximation gate |
| `ablation_compare.ipynb` | Optional read-only validation, then writes `comparison.csv` |

Each run notebook is independently controlled. Open the notebook, edit its configuration cell, and run it. The six run notebooks do not read each other's manifests or require the others to exist. For a comparable ablation, use the same `RAW_COMMIT_SHA` and `STUDY_ROOT` in all six; for unrelated runs, use different study roots.

### Fixed cohort

Every run notebook reads the attached `landslide-reproted` dataset file
`landslides_from_2018_to_2026.csv`, selects exactly `df.iloc[900:1000]`, preserves the CSV row
order, and uses its `id` column as the 100 incident IDs.

Before any computation, each notebook resolves the configured raw revision to a commit SHA, lists
the raw files at that immutable commit, and verifies that every selected incident has:

- `after.tif`
- `slope.tif`
- `sar_pre.tif`
- `sar_post.tif`
- either `planet_before.tif` or `gee_before.tif`

If any selected incident is incomplete, the notebook fails before computing. The notebooks scope Hub
listings to the raw input prefix and their own `<STUDY_ROOT>`; they do not enumerate, inspect, or
skip production `candidates/` output.

### Configure a run notebook

Open any `ablation_<variant>.ipynb` and edit its configuration cell directly:

```python
VARIANT_NAME = "baseline"
ABLATION_SWITCHES = {"modality": "fused", "slope": True, "slic": True, "bonferroni": True}

RAW_REPO = "sasudo2/landslides"
RAW_COMMIT_SHA = "main"  # resolved once to an immutable SHA at startup
OUTPUT_REPO = "sasudo2/landslides"
OUTPUT_REVISION = "main"
STUDY_ROOT = "ablations/study_01"
SOURCE_CSV_NAME = "landslides_from_2018_to_2026.csv"
COHORT_START = 900
COHORT_STOP = 1000
WORKERS = 4
```

Choose a `STUDY_ROOT` for the study, and reuse it to resume interrupted runs. `RAW_COMMIT_SHA`
accepts a full 40-character SHA, branch, or tag and defaults to `main`. Startup resolves it once, prints the immutable SHA, and
uses only that SHA for raw-file listings, downloads, and study metadata. For comparable runs,
copy the printed SHA into `RAW_COMMIT_SHA` in all six notebooks: using `main` independently
can select different commits if the repository changes between runs. The comparison notebook
still rejects different resolved raw commits. The output repository and branch must already
exist. Keep `VARIANT_NAME` and `ABLATION_SWITCHES` aligned with the notebook you are running
unless you intentionally create a new variant.

Each Kaggle run notebook needs:

1. **Settings -> Internet: On**
2. The `landslide-reproted` dataset attached, containing `landslides_from_2018_to_2026.csv`
3. The Kaggle secret `huggingface_token`, with read/write access to `output_repo`
4. The configuration cell edited and the setup cell executed before processing
5. All cells run in order

The notebooks install their own dependencies (`numpy`, `pandas`, `scipy`, `rasterio`,
`scikit-image`, `huggingface_hub`). No repository checkout or shared Python module is required.

### Run independently

Start any run notebook on its own. They may run sequentially, in parallel, or on different
schedules. Each notebook:

1. Validates its own variant name, switches, workers, and fixed cohort.
2. Resolves the raw commit and verifies all 100 incidents.
3. Restores its own existing incident records and skips incidents with remote candidate chips,
   plus successful zero-candidate incidents.
4. Processes missing incidents and updates only its own variant folder.

There is no cross-notebook preflight: no notebook reads a sibling manifest. Hub calls are retried
with bounded backoff for transient failures. Per-incident errors are recorded as failures, never
silently converted into zero-candidate results. A failed final commit raises and retains staged
outputs locally for recovery. Each notebook never
modifies a sibling folder, the study root itself, production `candidates/`, or a repository-wide
`.gitattributes`.

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
      result.json
      candidate_<n>/
        ... before, after, slope, mask chips ...
  optical_only/
  sar_only/
  no_slope/
  no_slic/
  no_bonferroni/
  comparison.csv               # optional comparison notebook only
```

`study.json` records this run's variant, switches, raw repository and commit SHA, detection algorithm
version, source CSV name and hash, `df.iloc[900:1000]` cohort declaration, ordered 100 incident IDs,
required raw-input policy, numerical settings, installed package versions, creation timestamp, and
run environment. The current detection algorithm is `ablation-detection-v2`; it excludes nodata
pixels and, for fused runs, only tests pixels valid in every selected modality. `run.json` records
execution-level details such as workers, start/finish times, status, and counts.

`candidate_status.csv` has one row for every incident, including zero-candidate and failed
incidents. `candidate_metadata.csv` is sorted and keeps the notebook-compatible ranking and
bounding-box columns. `diagnostics.json` records per-incident test counts, critical z thresholds,
mask pixels, and candidate counts. `summary.json` records successful/failed counts and candidate
counts.

### Rerun behavior

If a run is interrupted or fails, rerun the notebook with the same `STUDY_ROOT` and study
configuration. Existing candidate incidents are skipped without downloading raw rasters or
recomputing detection. Successful zero-candidate records are also reused; failed incidents without
remote candidate chips are retried. Other variants and production outputs are not used to decide
what to skip.

The notebook preserves existing candidate metadata, diagnostics, and counts in the merged summary.
All new candidates, incident `result.json` checkpoints (including zero detections), study metadata,
and summaries accumulate locally and are published together in one final commit. There is no
reservation commit or intermediate batch upload. An interruption before publication leaves new
incidents unpublished; rerunning skips previously published incidents and recomputes missing ones.
Do not run two copies of the same variant concurrently.

Each incident downloads raw imagery into its own private cache under this run's temporary working
directory. Those raw downloads are deleted immediately after chip export, or when download,
detection, or export fails. Remote raw imagery and shared Hugging Face caches are never deleted.
Candidate chips remain staged until the final commit, so provision enough disk space for all new
candidates. A final-commit failure preserves staged chips and summaries and reports their local
path; the notebook does not publish a success-shaped fallback or discard the files.

When resuming from an existing `study.json`, a branch/tag configuration reuses that study's original
raw commit, even if the branch has advanced. An explicitly configured different SHA or incompatible
study settings are rejected instead of mixing different experiments. Use a new study root for a
different experiment.

Legacy remote chips without metadata, incomplete chip sets, or an unverifiable study identity are
preserved and skipped with a warning. Missing metadata is not invented: `summary.json` marks
`metadata_complete=false`, lists `incomplete_incident_ids`, and reports the run as incomplete while
continuing to process missing incidents. Strict comparison rejects these incomplete runs; partial
comparison can report their known counts.

### Optional comparison notebook

Run `ablation_compare.ipynb` only if you want an aggregate. It is read-only until every validation
succeeds:

1. All six variant folders exist under the configured `STUDY_ROOT` with the expected files.
2. The shared study-identity fields are equivalent across all six `study.json` files.
3. Every variant declares its expected ablation switches.
4. Every study has the same ordered 100 incident IDs.
5. Every run is complete and has no failed incidents.

Only then does it write `<STUDY_ROOT>/comparison.csv`, with one row per variant: cohort size,
successful, failed, and unreported counts, incidents with candidates, total candidates, candidates
per successful incident, shared raw commit, identity-check result, completion status, and switches.
Set `ALLOW_PARTIAL = True` only when a partial diagnostic report is explicitly wanted; all six
study manifests must still exist, and the report marks incident records missing from the available
summary/status artifacts as `n_unreported`. Otherwise any failed or incomplete run blocks the
comparison.

The comparison reports candidate counts only. It does not measure accuracy, precision, recall, IoU,
or ground-truth correctness.

### Controls and interpretation

- These are six one-component-at-a-time runs, not a full factorial study. Each run notebook is
  independent; the optional comparison only aggregates their completed outputs.
- Baseline carries the `candidate_detection.ipynb` behavior: RGB-first optical IR-MAD, SAR VV/VH
  IR-MAD, geometric-mean fusion, a 10 m EPSG:4326 grid, valid-pixel masking, slope masking, SLIC
  segments, segment-level chi-square evidence with a normal-approximation gate, Bonferroni
  correction, percentile hysteresis, morphology, area/elongation filtering, ranking, and chip
  export.
- `optical_only` never fits or reads SAR for detection. `sar_only` never reads optical pixel values
  for detection or segmentation; optical rasters are still used for the analysis-grid reference and
  review chips. `no_slope` removes both the slope mask and the slope segmentation feature but still
  exports the slope review chip. `no_slic`
  replaces segments with pixelwise confidence and significance tests, with Bonferroni over the full
  grid pixel count. `no_bonferroni` retains the same normal-approximation z-test at `alpha=0.05`.
- Percentiles (99.7/93), slope cutoff (20 degrees), morphology, blob limits, ranking, chip padding,
  and IR-MAD settings retain the notebook defaults.
- Each selected modality must have more than 100 valid paired pixels. Otherwise the incident is
  recorded as failed rather than silently falling back to an available modality, so runs remain
  comparable. Fused runs also require more than 100 pixels valid in both modalities. Do not compare
  a failed incident as a zero detection.
  Insufficient coverage does not abort the notebook: successful outputs and failure details are
  saved, the notebook prints an explicit warning with incident IDs and valid-pixel counts, and the
  study remains incomplete. Summary/run records include `insufficient_coverage_incident_ids`.
  Strict comparison still rejects the incomplete study; other processing or upload errors still
  raise after accounting is saved. Reruns retry coverage failures without recomputing successes;
  unchanged raw data may continue to have insufficient coverage.
- These runs report candidate detection counts, not accuracy. Precision, recall, IoU, and scientific
  conclusions require independent ground-truth labels; candidate counts alone do not demonstrate
  detection quality.
