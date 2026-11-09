# Candidate-detection variable comparison

This table summarizes `data/cache/comparison (1).csv` for the six ablation variants. Each variant uses the same 100-incident cohort. The report checks study identity for all six runs; raw inputs are recorded as the same commit for the baseline and as identical input blobs for the other five variants.

| Variant | Modality used | Slope mask / feature | SLIC segmentation | Bonferroni correction | Successful / failed incidents | Incidents with ≥1 candidate | Candidates | Candidates per successful incident | Change vs. baseline | Metadata complete | Run complete |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Baseline | Optical + SAR (fused) | Yes | Yes | Yes | 99 / 1 | 87 | 2,582 | 26.08 | Yes | No |
| Optical only | Optical | Yes | Yes | Yes | 100 / 0 | 90 | 2,445 | 24.45 | No | No |
| SAR only | SAR | Yes | Yes | Yes | 99 / 1 | 90 | 2,611 | 26.37 | No | No |
| No slope | Optical + SAR (fused) | No | Yes | Yes | 99 / 1 | 95 | 2,975 | 30.05 | Yes | No |
| No SLIC | Optical + SAR (fused) | Yes | No | Yes | 99 / 1 | 77 | 678 | 6.85 | Yes | No |
| No Bonferroni | Optical + SAR (fused) | Yes | Yes | No | 99 / 1 | 87 | 2,673 | 27.00 | Yes | No |

## Interpretation

- **SLIC had the largest observed effect on candidate yield.** Removing SLIC reduced the count from 2,582 to 678 (−73.7%) and reduced incidents with at least one candidate from 87 to 77. This compares the SLIC-based segment workflow with the configured pixelwise alternative.
- **Removing the slope mask/feature increased output.** The no-slope run produced 2,975 candidates (+15.2%) and candidates in 95 incidents, compared with 2,582 candidates in 87 incidents for baseline. This describes yield, not accuracy.
- **Removing Bonferroni modestly increased output.** The uncorrected run produced 2,673 candidates (+3.5%), with the same 87 candidate-producing incidents as baseline.
- **The single-modality runs had similar candidate totals to baseline** (optical: 2,445; SAR: 2,611), but neither is a complete, metadata-complete run in this CSV.

## Completeness and limits

All six rows have `run_complete = no`. Five variants report 99 successful incidents and one failure; the failed incident is 74336, recorded in the run outputs as having zero valid paired SAR pixels, below the IR-MAD minimum of more than 100. The optical-only run reports 100 successful incidents but `metadata_complete = false`; its candidate metadata is incomplete. Thus, its count is diagnostic rather than a fully validated total. Candidate counts and candidates per successful incident are useful for comparing output volume, but the missing or failed incident and incomplete metadata prevent a complete scientific ablation comparison. Do not interpret missing candidates as zero detections.

The CSV contains no independent ground-truth matching or accuracy measures. These counts cannot establish which configuration detects landslides more accurately. Precision, recall, IoU, or site-level agreement require labeled reference data. The variants change one detection component at a time relative to baseline: modality (optical/SAR), slope, segmentation (SLIC), or significance correction (Bonferroni). The numerical changes are descriptive and remain subject to the incomplete-run caveat above.
