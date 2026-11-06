# Reviewer Feedback Action Plan

## Priority 1: Resolve the main technical concerns

- [ ] Reconcile dataset counts throughout the manuscript. Confirm the correct totals for landslide and non-landslide candidates, including the reported 247/248 and 5,594/5,595 discrepancies. Distinguish dated analysis snapshots from current dataset totals.
- [ ] Make the method reproducible. State the equations and assumptions for optical–SAR confidence fusion, chi-square evidence aggregation, Bonferroni correction, and candidate ranking.
- [ ] Report implementation parameters and how they were selected, including minimum slope, SLIC, hysteresis thresholds, geometric filters, and IR-MAD convergence criteria.
- [ ] Validate candidate detection quantitatively against an independent landslide inventory. Report appropriate metrics such as precision, recall, false-positive rate, and IoU where applicable.
- [ ] Add comparisons and ablations. Compare against relevant existing workflows or baselines, and evaluate individual sensors and fusion strategies.
- [ ] Demonstrate downstream dataset utility. Train and evaluate a segmentation baseline, such as U-Net, using the annotated masks. Document the data split and metrics, and avoid data leakage.

## Priority 2: Strengthen applicability and evidence

- [ ] Evaluate independent incidents across different regions, seasons, terrain, and sensor conditions. Discuss coordinate uncertainty, cloud cover, SAR layover/shadow, and missed detections.
- [ ] Test fusion failure modes. Assess whether requiring strong optical and SAR signals misses true landslides, and compare with alternative or more tolerant fusion approaches.
- [ ] Quantify operational effort and cost. Report processing time, computational requirements, degree of automation, and manual-review effort relative to a reasonable baseline.
- [ ] Explain dataset coverage and imbalances across geography, time, and terrain. Report progress toward processing more of the 4,138 incidents and expanding beyond the 164 pixel-level annotations.
- [ ] Investigate and document the 170 labels reported outside province boundaries. Correct coordinates where justified and explain how coordinate errors are handled.

## Priority 3: Improve positioning and presentation

- [ ] Clarify the workflow diagram, showing incident acquisition, imagery, detection, filtering, human review, annotation, and dataset release.
- [ ] Strengthen the novelty comparison with existing datasets and workflows, covering reproducibility, annotation quality, coverage, accessibility, candidate localization, and review effort.
- [ ] Update the literature review with relevant recent work, especially from 2021–2026, on landslide datasets, optical–SAR fusion, deep-learning mapping, and reproducible geospatial workflows. Retain older references that establish foundational methods.
- [ ] Verify that every in-text citation has a bibliography entry and vice versa. Check bibliographic details, DOIs, publication years, and dataset URLs.
- [ ] Revise claims and limitations to distinguish demonstrated results from future potential; discuss quarterly mosaic timing and the current scale of completed processing.
- [ ] Prepare a reviewer response mapping each comment to a manuscript change, new experiment, or clearly stated limitation.

## Suggested order

1. Reconcile dataset counts and document the method and parameters.
2. Run independent validation, ablations, and downstream baseline experiments.
3. Assess operational costs, coverage, and data-quality limitations.
4. Update the literature, comparisons, manuscript narrative, and reviewer response using the resulting evidence.
