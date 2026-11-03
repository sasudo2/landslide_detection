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

The [ablation scripts](ablation/) port the numerical pipeline from
`candidate_detection.ipynb` without changing the notebook or production `candidates/`.
Run them from the repository root with Python 3.10+:

```bash
python -m pip install -r ablation/requirements.txt
# Authenticate using HF_TOKEN (write access) or `hf auth login`.
python -m ablation.pipeline --study-root ablations/study_01 --limit 10
```

The command runs all six experiments sequentially and uploads results to
`sasudo2/landslides`, under the supplied study folder, including a consolidated
`comparison.csv` with counts for all six variants. Omit `--limit` to process
all complete incidents. The destination repo and branch must already exist.
Use `--output-repo owner/dataset` and `--output-revision branch` to change them.
Raw inputs default to `--repo sasudo2/landslides --raw-revision main`.
No download, computation, or upload occurs merely by importing a script.

Individual entry points (use the **same arguments** for every run in a study):

```bash
python -m ablation.baseline      --study-root ablations/study_02 --limit 10
python -m ablation.optical_only  --study-root ablations/study_02 --limit 10
python -m ablation.sar_only      --study-root ablations/study_02 --limit 10
python -m ablation.no_slope      --study-root ablations/study_02 --limit 10
python -m ablation.no_slic       --study-root ablations/study_02 --limit 10
python -m ablation.no_bonferroni --study-root ablations/study_02 --limit 10
```

| Experiment | Change evidence | Slope mask/feature | SLIC | Significance gate |
|---|---|---|---|---|
| baseline | Optical + SAR geometric mean | On | On | Bonferroni |
| optical_only | RGB IR-MAD only | On | On | Bonferroni |
| sar_only | VV/VH IR-MAD only | On | On | Bonferroni |
| no_slope | Optical + SAR | Off | On | Bonferroni |
| no_slic | Optical + SAR | On | Off (pixelwise) | Bonferroni |
| no_bonferroni | Optical + SAR | On | On | Uncorrected alpha=0.05 |

### Running on Kaggle

1. Commit and push `ablation/` to GitHub (Kaggle clones the public repo).
2. In a Kaggle notebook: **Settings → Internet: On**, and **Add-ons → Secrets** attach
   `huggingface_token` (the same write-access secret `candidate_detection.ipynb` uses).
   The scripts read it automatically; `HF_TOKEN` takes precedence if set.
3. Run these cells:

```python
!git clone --depth 1 https://github.com/sasudo2/landslide_detection.git /kaggle/working/repo
%cd /kaggle/working/repo
!pip install -q rasterio "scikit-image>=0.19,<2"
```

```python
# Quick check on 5 incidents, all six experiments:
!python -m ablation.pipeline --study-root ablations/kaggle_test --limit 5
```

To restrict a study to a slice, e.g. the 900th–1000th incident (101 incidents)
with complete raw data on HF, sorted by ID (same counting as `download_incidents.sh 900 1000`):

```python
!python -m ablation.pipeline --study-root ablations/range_900_1000 --range 900 1000
```

The range is resolved once and frozen in `study.json`; if you split variants across
sessions, repeat the same `--range 900 1000` and `--study-root` each time. The run
errors out (rather than silently shrinking) if fewer than END complete incidents exist.

Use `!python -m ...` rather than importing and calling `main()` in the kernel
(the kernel's own arguments would be parsed). Kaggle sessions stop after about
12 hours, so for the full cohort split the experiments across sessions with the
**same** study root; the first run freezes the cohort, and later sessions reuse it:

```python
!python -m ablation.pipeline --study-root ablations/study_01 --variant baseline
# next session: --variant optical_only, then sar_only, no_slope, no_slic, no_bonferroni
```

`comparison.csv` is written only by `--variant all`; per-run summaries are always
uploaded. If a session dies mid-experiment, that experiment folder is refused on
rerun—start a new study root (all experiments must share one study).

### Controls and interpretation

- These are six one-component-at-a-time runs, **not** a full factorial study.
- The first run freezes the raw-data commit, complete-input cohort, pipeline
  fingerprint, dependency versions, and settings in `<study-root>/study.json`. Subsequent scripts reuse
  that commit even as uploads advance the destination branch. Use
  `--incident-ids 123 456` for an explicit subset, `--range 900 1000` for the
  900th–1000th complete incidents by ID, or `--limit N` for the first N
  complete IDs in numeric order. All runs require complete optical, SAR, and slope
  files so missing modalities cannot change the comparison cohort.
- All variants use the notebook's common 10 m grid derived from optical-before
  geometry. SAR-only reads no optical values for detection: it uses SAR-post
  features for SLIC instead of optical-post RGB. Optical-only never fits SAR.
  Optical before/after values are still exported as review chips in every variant.
- Disabling slope also removes its SLIC feature; disabling SLIC replaces
  superpixel averaging with pixelwise confidence and significance tests.
  Bonferroni divides alpha by the number of segments (including out-of-terrain
  segments, as in the notebook), or by the full grid pixel count without SLIC.
  Disabling Bonferroni retains the same normal-approximation z-test at alpha=0.05.
  This approximation and its pixel-independence assumption are inherited from the
  notebook, not a claim of calibrated real-world error control.
- Percentiles (99.7/93), slope cutoff (20 degrees), morphology, blob limits,
  ranking, chip padding, and IR-MAD settings retain the notebook defaults.
  Unlike its available-modality fallback, the scripts explicitly fail incidents
  with <=100 valid paired pixels in any selected modality. Do not compare failed
  incidents as zero detections; compare only incidents successful in every run.
- Each run reports candidate counts, not accuracy. Precision, recall, IoU, and
  scientific conclusions require independent ground-truth labels; counts alone
  do not demonstrate detection quality.

### Results and safety

Each `<study-root>/<experiment>/` contains:

- `run.json`: switches plus study inputs, numerical settings, and package versions.
- `candidate_status.csv`: one row per incident, including zero detections/errors.
- `candidate_metadata.csv`: notebook-compatible ranking and bounding-box columns,
  with headers even if there are no candidates.
- `diagnostics.json`: per-incident test counts, critical z thresholds, mask pixels.
- `summary.json`: successful/failed incident counts and candidate counts.
- `incident_{ID}/candidate_{N}/incident_{ID}_candidate_{N}_{before,after,slope,mask}.tif`:
  native-resolution optical chips plus analysis-grid slope and mask chips.

Local copies remain in `ablation_results/<study-root>/` (change with `--output-dir`).
Within each experiment, incidents are processed in parallel (`--workers`, default 4,
like the notebook's `MAX_WORKERS`); experiments themselves still run one after another.
Chip uploads are batched into commits of about 100 files from the main thread, and
CSV/JSON outputs are sorted by incident ID regardless of completion order. Results are
identical to `--workers 1`. Lower `--workers` if Kaggle runs out of memory.
Uploads commit chips in batches and then the variant summaries; any failure
(including a failed upload batch, which marks those incidents failed)
is logged with a traceback and recorded, and the command exits unsuccessfully.
Partially uploaded runs are not silently reused. A variant folder is reserved
before processing, and existing local/remote variants are refused: choose a new
study root to retry or change the cohort/settings/code. Run scripts sequentially,
not concurrently against the same study folder. The scripts never modify
repository-wide `.gitattributes` or delete remote data.

Validation (synthetic rasters and mocked Hub calls; no remote writes):

```bash
python -m unittest ablation.test_pipeline
```