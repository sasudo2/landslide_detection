# Landslide susceptibility detection

This repository processes reported landslide incidents into standardized raster inputs and candidate chips for downstream review.

The active processing flow is implemented in:
1. `notebooks/pipeline/stage_1_data_acquisition/incident_download.ipynb` (Planet quarterly composite download + GEE support layers)
2. `notebooks/pipeline/stage_2_candidate_collection/` (candidate detection, candidate download, then spatial deduplication)
3. `notebooks/pipeline/stage_3_annotation_and_png_export/` (annotation deduplication, then cropped-image download and PNG export)

`notebooks/legacy/planet_order_creation.ipynb` is not part of the current flow and can be ignored.

Hugging Face dataset repo `sasudo2/landslides` is the storage backend.

## Repository layout

| Location | Purpose |
|---|---|
| `notebooks/pipeline/stage_1_data_acquisition/` | Active incident download and GEE support-layer pipeline |
| `notebooks/pipeline/stage_2_candidate_collection/` | Candidate detection, download, and spatial deduplication |
| `notebooks/pipeline/stage_3_annotation_and_png_export/` | Annotation deduplication and image/PNG export |
| `notebooks/experiments/ablation/` | Ablation run notebooks and comparison |
| `notebooks/analysis/` | Detection tuning and annotation analysis |
| `notebooks/data_preparation/` | Annotation and input-data preparation |
| `notebooks/legacy/` | Superseded or historical workflows |
| `scripts/` | Analysis, download, ablation, and maintenance utilities |
| `plugins/landslide_annotator/` | QGIS annotation plugin |
| `data/annotations/` | Annotation inputs and labeled CSVs |
| `data/reference/` | Incident source inventory |
| `data/cache/` | Local elevation cache |
| `docs/` | Pipeline specifications, runbooks, and project notes |
| `tests/` | Automated regression tests |

Generated imagery and ablation outputs (for example `incidents/` and
`ablation_candidates/`) remain separate from the tracked source tree.
Run repository-local notebooks that use relative data paths from the repository root.

## Current status

The incident download logic uses precomputed **Planet quarterly mosaic composites** as the primary optical source:
- before/after optical imagery is fetched directly from Planet's Mosaics/quads API per incident (no per-scene order submission or polling),
- Planet's own compositing already removes cloud/shadow and normalizes color across scenes,
- no per-scene order creation, order log, per-incident quality-report, or run manifest is used anymore.

## Stage 1: `notebooks/pipeline/stage_1_data_acquisition/incident_download.ipynb`

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

## Stage 2: Candidate collection

Run the assets in this order:
1. `notebooks/pipeline/stage_2_candidate_collection/candidate_detection.ipynb` detects candidate chips and uploads them to Hugging Face.
2. `notebooks/pipeline/stage_2_candidate_collection/download_candidates.sh` downloads candidate chips locally for review/annotation.
3. `notebooks/pipeline/stage_2_candidate_collection/deduplicate_candidates.py` removes spatially repeated candidate folders after previewing the report.

Purpose:
- Build ranked candidate chips per incident from multi-source raster evidence.

Current behavior highlights:
- Uses available before/after/support rasters (treating the optical pair as RGB-first since the quarterly composites are color/visual mosaics).
- Produces candidate chip outputs and metadata.
- Uploads candidate artifacts to Hugging Face in batched commit operations.
- Uploads `candidate_status.csv` and `candidate_metadata.csv` together in one commit.

## Stage 3: Annotation and PNG export

Crop windows extending outside a raw TIFF are shifted inward to its edge without changing their pixel dimensions. Shifts are logged and the output georeferencing reflects the moved crop. A crop larger than the raw TIFF is reported as a processing error rather than resized.

If both Planet and GEE after-images are missing, the exporter reports and skips that landslide without stopping the notebook. Missing-source skips are counted separately from existing cropped TIFFs.

Run the assets in this order:
1. `notebooks/pipeline/stage_3_annotation_and_png_export/annotation_dedup_build.ipynb` resizes and deduplicates annotated rows, appending new records to the landslide/non-landslide CSVs.
2. `notebooks/pipeline/stage_3_annotation_and_png_export/landslide_download.ipynb` reads `landslide.csv`, skips candidates whose cropped TIFF already exists under Hugging Face `landslide/`, then crops the raw Planet after-image or falls back to `incident_{ID}_gee_after.tif` if present. It uploads individual TIFFs to `landslide/` and one `landslide_png/landslide_png_yyyymmdd_hhmmss.zip` containing that run's PNGs.

Attach `landslide.csv` as a Kaggle dataset and update `LANDSLIDE_CSV` in the notebook to its mounted path. TIFFs and the ZIP are uploaded in one Hugging Face commit; existing TIFFs are checked before any raw-image downloads. The current Stage 1 notebook creates `gee_before.tif`, not `gee_after.tif`, so this fallback works only when a GEE after-image was uploaded separately. The notebook reads the `huggingface_token` Kaggle secret and does not need a Planet API key.

## Related documentation

- `docs/pipeline/REFINED_PIPELINE_SPEC.md`: authoritative requirements/spec checklist.
- `docs/pipeline/MOSAIC_SOURCE_QUALITY_RUNBOOK.md`: operational runbook for quarterly composite selection.
- `notebooks/legacy/`: historical workflows and broader methodology context.

## Candidate-detection ablation study

The study runs as seven self-contained Kaggle notebooks: six independent run notebooks and one optional comparison notebook. Each run notebook contains its own pipeline code and configuration. No notebook imports repository modules, executes another notebook, or depends on a shared generated artifact.

| Notebook | Role |
|---|---|
| `notebooks/experiments/ablation/ablation_baseline.ipynb` | Optical + SAR, slope mask/feature, SLIC, Bonferroni |
| `notebooks/experiments/ablation/ablation_optical_only.ipynb` | Optical only |
| `notebooks/experiments/ablation/ablation_sar_only.ipynb` | SAR only |
| `notebooks/experiments/ablation/ablation_no_slope.ipynb` | No slope mask and no slope segmentation feature |
| `notebooks/experiments/ablation/ablation_no_slic.ipynb` | No SLIC; pixelwise evidence and significance gate |
| `notebooks/experiments/ablation/ablation_no_bonferroni.ipynb` | Uncorrected `alpha=0.05`; same normal-approximation gate |
| `notebooks/experiments/ablation/ablation_compare.ipynb` | Optional read-only validation, then writes `comparison.csv` |

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

### Download one ablation variant locally

Use `scripts/ablation/download_ablation_candidates.sh` independently of the production downloader:

```bash
# Requires Python 3 and huggingface_hub; use HF_TOKEN or an existing HF login.
./scripts/ablation/download_ablation_candidates.sh optical_only
./scripts/ablation/download_ablation_candidates.sh no_slic 1 10 --outdir ./review/no_slic
./scripts/ablation/download_ablation_candidates.sh baseline --study-root ablations/study_02 --revision main
./scripts/ablation/download_ablation_candidates.sh --help
```

The required variant is one of `baseline`, `optical_only`, `sar_only`, `no_slope`, `no_slic`,
or `no_bonferroni`. Optional start/end indices are 1-based, inclusive positions among numerically
sorted incidents **with candidate TIFFs**, not incident IDs. A single index selects one incident.
The default destination is `./ablation_candidates/<study-root>/<variant>/`; `--outdir` selects
an exact destination (use separate destinations for different studies/variants).
`--repo` overrides the default dataset repository `sasudo2/landslides`.

The script pins the requested revision once, lists only the selected variant, and downloads
its selected incident folders (chips and checkpoints), plus the available run-level manifests,
CSVs, diagnostics, and summary. The remote study/variant prefix is stripped locally, leaving
`incident_<id>/candidate_<n>/...`. Run-level metadata still describes the full variant when
downloading a subset. Variants with no candidates can still download their run metadata.
Selected files are fetched directly with up to four concurrent downloads, avoiding a
repository-wide snapshot scan and per-file glob filtering. File progress is printed as
downloads are copied into the destination.
Hugging Face cache reuse and bounded transient retries support reruns; missing local files are
restored, changed files are refreshed, and identical files are not recopied. No external
download log suppresses restoration. Other variants, production candidates, and raw imagery
are not downloaded.

### Remove repeating candidate folders before annotation

`notebooks/pipeline/stage_2_candidate_collection/deduplicate_candidates.py` applies spatial overlap filtering to one local folder containing
`incident_*/candidate_*` directories. It requires `rasterio`.

```bash
# Preview duplicates and write candidate_dedup_report.csv without deleting anything.
python3 notebooks/pipeline/stage_2_candidate_collection/deduplicate_candidates.py ./review/no_slic

# Permanently delete duplicate candidate folders.
python3 notebooks/pipeline/stage_2_candidate_collection/deduplicate_candidates.py ./review/no_slic --delete
```

The script transforms each after raster's actual bounds to WGS84 and compares those boxes
at their original geographic locations and sizes, keeping the first candidate in numeric incident/candidate order when IoU is
at least 0.50. It compares across all incidents; labels and event dates are not considered.
`--iou` and `--report` override the defaults. Like the annotation notebook,
this is greedy spatial filtering, not proof that two candidates depict the same event.
Unlike the annotation notebook, boxes are not normalized to 1,000 m. No boxes or raster chips
are cropped, resized, or rewritten. All candidates are scanned successfully and a report is written
before deletion starts; failures are explicit, and already-completed deletions cannot be undone.
The CSV maps each proposed duplicate to its retained candidate and overlap score.

Use an annotation-only copy: study CSVs, checkpoints, summaries, and existing annotations are
left unchanged and will no longer describe the reduced folder contents. Remote data is never
modified. Downloading again can restore deleted candidates. Do not combine variants into one
folder or run downloads/annotation concurrently with deletion.

### Final ablation table from annotations

After all six variants are downloaded, deduplicated, and labelled, `scripts/analysis/analyze_ablation.py` builds
the final ablation table (standard library only):

```bash
python3 scripts/analysis/analyze_ablation.py                                   # ablation_candidates/ablations/study_01
python3 scripts/analysis/analyze_ablation.py <study_dir> --outdir <dir> --site-iou 0.25 --bootstrap 5000
```

Each variant folder needs `candidate_status.csv` and `landslide_identification.csv` (or
`landslides_identification.csv`). Only incidents with status `ok` in every variant are compared.
Because labels exist only for candidates kept by `deduplicate_candidates.py`, the same global
greedy IoU ≥ 0.50 rule is re-applied to every labelled set by using the label bounding boxes. This
has no effect on variants that were already deduplicated and makes variants that were labelled
without deduplication comparable. Duplicates take no label from the candidate they duplicate.

There is no exhaustive ground truth, so recall is relative. Landslide-labelled boxes from all
variants are pooled and merged into sites at IoU ≥ `--site-iou` (default 0.1), and each variant
is scored on the sites it recovers. The table reports raw/unique candidates, TP, precision,
relative recall, F1, incidents hit, and candidates reviewed per TP. Each component's contribution
is reported as baseline minus ablated, with paired sites lost/gained, an exact McNemar p-value,
and incident-level bootstrap 95% CIs for ΔRecall and ΔF1. Outputs go to
`<study_dir>/ablation_results/`: `ablation_table.csv`, `ablation_table.md`, and
`ablation_sites.csv`, which lists every pooled site and the variants that found it.

### Tune detection variables against the pooled sites

`notebooks/analysis/detection_tuning.ipynb` is a self-contained Kaggle notebook that searches the full model's detection
variables for values that cut false positives while keeping or raising recall. It reuses the
`ablations/study_01` raw commit (`8b2c2d92…`) and cohort, and embeds the 66 pooled sites from
`ablation_sites.csv` as ground truth (each site's member detection boxes).

- IR-MAD evidence is computed once per incident with code copied verbatim from
  `ablation_baseline.ipynb`; baseline parameters reproduce the ablation run's candidates exactly.
- A multi-objective Optuna search (maximise sites found, minimise false positives) tunes fusion
  weight, slope threshold, SLIC size/compactness, hysteresis percentiles, alpha, blob filters,
  candidate cap, a minimum-severity filter, and the ranking score.
- Candidates are deduplicated like `analyze_ablation.py`; a candidate is a TP when its detection
  box has IoU ≥ `MATCH_IOU` (default 0.2) with a site member.
- Incidents are split into tuning and hold-out sets (overlapping incidents stay together); only the
  hold-out rows of `comparison.csv` show whether the selected values generalise.

Enable internet and run all cells; no token is required for the public raw dataset. Outputs go to
`/kaggle/working/tuning_results/` (`comparison.csv`, `pareto.csv`, `trials.csv`,
`recommended_settings.json`, `selected_candidates.csv`). The 66 sites are not exhaustive, so label
unmatched `selected_candidates.csv` rows before trusting the reported precision.

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

Run `notebooks/experiments/ablation/ablation_compare.ipynb` only if you want an aggregate. It is read-only until every validation
succeeds:

1. All six variant folders exist under the configured `STUDY_ROOT` with the expected files.
2. The shared study-identity fields are equivalent across all six `study.json` files. Different
   raw commit SHAs are accepted only if Hub blob IDs match for all 500 selected raw inputs.
3. Every variant declares its expected ablation switches.
4. Every study has the same ordered 100 incident IDs.
5. In strict mode (`ALLOW_PARTIAL = False`), every run is complete and has no failed incidents.

Only then does it write `<STUDY_ROOT>/comparison.csv`, with one row per variant: cohort size,
successful, failed, and unreported counts, incidents with candidates, total candidates, candidates
per successful incident, shared raw commit, identity-check result, completion status, and switches.
`ALLOW_PARTIAL = True` is the default, allowing diagnostic reports for runs with coverage failures
or incomplete legacy outputs. All six matching study manifests must still exist; conflicting
identities and malformed accounting still block publication. Incomplete runs emit explicit
warnings and are marked `run_complete=no`, with `run_status`, `metadata_complete`, failed counts,
and missing incident records reported as `n_unreported`. Set `ALLOW_PARTIAL = False` to require
complete runs. Partial totals are not a complete or necessarily comparable ablation result:
different runs may have different successful subsets.

Publishing outputs to the raw-data repository advances its commit SHA even when raw imagery is
unchanged. When SHAs differ, comparison queries file metadata at each recorded immutable commit
(without downloading imagery) and verifies the selected before, after, slope, and two SAR files
for every cohort incident. The report preserves each original `raw_commit_sha` and records
`raw_identity_check` as `same_commit` or `identical_input_blobs`. Changed or unverifiable inputs
still block publication, even in partial mode. If raw inputs really changed, rerun affected variants
with one explicit `RAW_COMMIT_SHA` in a new `STUDY_ROOT`; do not edit saved manifests to hide differences.

The comparison reports candidate counts only. It does not measure accuracy, precision, recall, IoU,
or ground-truth correctness.

### Controls and interpretation

- These are six one-component-at-a-time runs, not a full factorial study. Each run notebook is
  independent; the optional comparison only aggregates their completed outputs.
- Baseline carries the `notebooks/pipeline/stage_2_candidate_collection/candidate_detection.ipynb` behavior: RGB-first optical IR-MAD, SAR VV/VH
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
