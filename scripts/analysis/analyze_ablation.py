#!/usr/bin/env python3
"""Build the final ablation table from labelled ablation candidates.

Each variant's ``landslide_identification.csv`` labels only candidates that survived spatial
deduplication (see ``deduplicate_candidates.py``). Variants are therefore compared on unique
candidates: the same greedy IoU rule is re-applied to every variant's labelled set, which is a
no-op for already-deduplicated variants and normalises any variant that was never deduplicated.

There is no exhaustive ground truth, so recall is *relative*: every landslide-labelled box from
every variant is pooled and merged into unique landslide sites, and each variant is scored on the
fraction of pooled sites it recovers.
"""

import argparse
import csv
import json
import math
import random
import re
import sys
from pathlib import Path

VARIANTS = {
    "baseline": ("None (full model)", "Optical+SAR fusion, slope, SLIC, Bonferroni"),
    "optical_only": ("SAR", "Optical only"),
    "sar_only": ("Optical", "SAR only"),
    "no_slope": ("Slope", "No slope mask/feature"),
    "no_slic": ("SLIC", "Pixelwise, no SLIC superpixels"),
    "no_bonferroni": ("Bonferroni", "Uncorrected alpha=0.05"),
}
EXPECTED_SWITCHES = {
    "baseline": {"modality": "fused", "slope": True, "slic": True, "bonferroni": True},
    "optical_only": {"modality": "optical", "slope": True, "slic": True, "bonferroni": True},
    "sar_only": {"modality": "sar", "slope": True, "slic": True, "bonferroni": True},
    "no_slope": {"modality": "fused", "slope": False, "slic": True, "bonferroni": True},
    "no_slic": {"modality": "fused", "slope": True, "slic": False, "bonferroni": True},
    "no_bonferroni": {"modality": "fused", "slope": True, "slic": True, "bonferroni": False},
}
LABEL_FILES = ("landslide_identification.csv", "landslides_identification.csv")
POSITIVE = "landslide"
NEGATIVE = "not_landslide"

TABLE_FIELDS = [
    "variant", "component_removed", "description", "n_incidents", "n_raw_candidates",
    "n_unique_candidates", "n_dedup_removed_here", "tp", "fp", "precision", "sites_found",
    "relative_recall", "f1", "incidents_hit", "incident_recall", "candidates_per_tp",
    "sites_lost_vs_baseline", "sites_gained_vs_baseline", "mcnemar_p", "delta_precision",
    "delta_recall", "delta_recall_ci_low", "delta_recall_ci_high", "delta_f1", "delta_f1_ci_low",
    "delta_f1_ci_high", "recall_drop_pct",
    "f1_drop_pct",
]


def iou(first, second):
    left, bottom, right, top = first
    other_left, other_bottom, other_right, other_top = second
    intersection = max(0, min(right, other_right) - max(left, other_left)) * max(
        0, min(top, other_top) - max(bottom, other_bottom)
    )
    union = (
        (right - left) * (top - bottom)
        + (other_right - other_left) * (other_top - other_bottom)
        - intersection
    )
    return intersection / union if union > 0 else 0.0


def candidate_number(name):
    match = re.fullmatch(r"candidate_(\d+)", name)
    if not match:
        raise ValueError(f"Unexpected candidate name: {name!r}")
    return int(match[1])


def read_csv(path):
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def load_variant(folder):
    label_paths = [folder / name for name in LABEL_FILES if (folder / name).exists()]
    if len(label_paths) != 1:
        raise ValueError(f"Expected exactly one of {LABEL_FILES} in {folder}")
    labels = []
    for row in read_csv(label_paths[0]):
        labels.append({
            "incident": int(row["incident"]),
            "candidate": row["candidate"],
            "number": candidate_number(row["candidate"]),
            "bounds": tuple(float(row[k]) for k in ("min_lon", "min_lat", "max_lon", "max_lat")),
            "label": row["label"].strip(),
        })
    status = {int(row["incident_id"]): row for row in read_csv(folder / "candidate_status.csv")}
    switches = None
    summary = folder / "summary.json"
    if summary.exists():
        switches = json.loads(summary.read_text(encoding="utf-8")).get("switches")
    return {"labels": labels, "status": status, "switches": switches}


def deduplicate(labels, threshold):
    """Global greedy dedup in (incident, candidate) order, matching deduplicate_candidates.py."""
    kept = []
    for row in sorted(labels, key=lambda r: (r["incident"], r["number"])):
        if not any(iou(row["bounds"], other["bounds"]) >= threshold for other in kept):
            kept.append(row)
    return kept


def build_sites(positives, threshold):
    """Union-find over pooled positive boxes; returns a list of member-index sets."""
    parent = list(range(len(positives)))

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for i in range(len(positives)):
        for j in range(i + 1, len(positives)):
            if iou(positives[i]["bounds"], positives[j]["bounds"]) >= threshold:
                parent[find(i)] = find(j)
    groups = {}
    for index in range(len(positives)):
        groups.setdefault(find(index), []).append(index)
    return sorted(groups.values(), key=lambda g: min((positives[i]["incident"], positives[i]["number"]) for i in g))


def ratio(numerator, denominator):
    return numerator / denominator if denominator else 0.0


def f1_score(precision, recall):
    return ratio(2 * precision * recall, precision + recall)


def mcnemar_exact(lost, gained):
    n = lost + gained
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, k) for k in range(min(lost, gained) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def percentile_ci(values, level=0.95):
    if not values:
        return "", ""
    values = sorted(values)
    tail = (1 - level) / 2
    return values[round(tail * (len(values) - 1))], values[round((1 - tail) * (len(values) - 1))]


def metrics(per_incident, incidents, site_incident, found_sites):
    unique = sum(per_incident[i][0] for i in incidents)
    tp = sum(per_incident[i][1] for i in incidents)
    weights = {}
    for incident in incidents:
        weights[incident] = weights.get(incident, 0) + 1
    total_sites = sum(weights.get(inc, 0) for inc in site_incident)
    found = sum(weights.get(site_incident[s], 0) for s in found_sites)
    recall = ratio(found, total_sites)
    return recall, f1_score(ratio(tp, unique), recall)


def analyze(study_dir, dedup_iou=0.5, site_iou=0.1, bootstrap=2000, seed=0):
    warnings = []
    data = {}
    for variant in VARIANTS:
        folder = study_dir / variant
        if not folder.is_dir():
            raise FileNotFoundError(f"Missing variant folder: {folder}")
        data[variant] = load_variant(folder)
        switches = data[variant]["switches"]
        if switches is not None and switches != EXPECTED_SWITCHES[variant]:
            warnings.append(f"{variant}: switches {switches} != expected {EXPECTED_SWITCHES[variant]}")

    incident_sets = [
        {i for i, row in d["status"].items() if row["status"] == "ok"} for d in data.values()
    ]
    common = set.intersection(*incident_sets)
    excluded = sorted(set.union(*incident_sets, *({r["incident"] for r in d["labels"]} for d in data.values())) - common)
    if excluded:
        warnings.append(f"Excluded incidents not 'ok' in every variant: {excluded}")

    unique = {}
    for variant, d in data.items():
        labels = [row for row in d["labels"] if row["incident"] in common]
        other = sorted({row["label"] for row in labels} - {POSITIVE, NEGATIVE})
        if other:
            warnings.append(f"{variant}: labels {other} counted as negatives")
        kept = deduplicate(labels, dedup_iou)
        d["n_labelled"] = len(labels)
        d["n_raw"] = sum(int(d["status"][i]["n_candidates"] or 0) for i in common)
        unique[variant] = kept
        if len(labels) != len(kept):
            warnings.append(
                f"{variant}: {len(labels) - len(kept)} labelled candidates removed by IoU>={dedup_iou} "
                "dedup (variant was not deduplicated before labelling)"
            )

    positives = [
        dict(row, variant=variant) for variant, rows in unique.items() for row in rows if row["label"] == POSITIVE
    ]
    sites = build_sites(positives, site_iou)
    site_incident = [min(positives[i]["incident"] for i in group) for group in sites]
    found = {v: {s for s, group in enumerate(sites) if any(positives[i]["variant"] == v for i in group)} for v in VARIANTS}
    landslide_incidents = {row["incident"] for row in positives}

    per_incident = {}
    for variant, rows in unique.items():
        counts = {i: [0, 0] for i in common}
        for row in rows:
            counts[row["incident"]][0] += 1
            counts[row["incident"]][1] += row["label"] == POSITIVE
        per_incident[variant] = counts

    rng = random.Random(seed)
    incidents = sorted(common)
    samples = [[rng.choice(incidents) for _ in incidents] for _ in range(bootstrap)]
    base_samples = [metrics(per_incident["baseline"], s, site_incident, found["baseline"]) for s in samples]

    rows = []
    base = None
    for variant, (component, description) in VARIANTS.items():
        kept = unique[variant]
        tp = sum(row["label"] == POSITIVE for row in kept)
        precision = ratio(tp, len(kept))
        recall = ratio(len(found[variant]), len(sites))
        f1 = f1_score(precision, recall)
        hit = {row["incident"] for row in kept if row["label"] == POSITIVE}
        row = {
            "variant": variant, "component_removed": component, "description": description,
            "n_incidents": len(common), "n_raw_candidates": data[variant]["n_raw"],
            "n_unique_candidates": len(kept), "n_dedup_removed_here": data[variant]["n_labelled"] - len(kept),
            "tp": tp, "fp": len(kept) - tp, "precision": precision, "sites_found": len(found[variant]),
            "relative_recall": recall, "f1": f1, "incidents_hit": len(hit),
            "incident_recall": ratio(len(hit), len(landslide_incidents)),
            "candidates_per_tp": ratio(len(kept), tp) if tp else math.inf,
        }
        if variant == "baseline":
            base = row
            row.update({key: "" for key in TABLE_FIELDS if key not in row})
        else:
            lost = len(found["baseline"] - found[variant])
            gained = len(found[variant] - found["baseline"])
            boot = [
                (b[0] - v[0], b[1] - v[1])
                for b, v in zip(base_samples, (
                    metrics(per_incident[variant], s, site_incident, found[variant]) for s in samples
                ))
            ]
            recall_ci = percentile_ci([d[0] for d in boot])
            f1_ci = percentile_ci([d[1] for d in boot])
            row.update({
                "sites_lost_vs_baseline": lost, "sites_gained_vs_baseline": gained,
                "mcnemar_p": mcnemar_exact(lost, gained),
                "delta_precision": base["precision"] - precision,
                "delta_recall": base["relative_recall"] - recall,
                "delta_recall_ci_low": recall_ci[0], "delta_recall_ci_high": recall_ci[1],
                "delta_f1": base["f1"] - f1,
                "delta_f1_ci_low": f1_ci[0], "delta_f1_ci_high": f1_ci[1],
                "recall_drop_pct": 100 * ratio(base["relative_recall"] - recall, base["relative_recall"]),
                "f1_drop_pct": 100 * ratio(base["f1"] - f1, base["f1"]),
            })
        rows.append(row)

    site_rows = []
    for index, group in enumerate(sites):
        members = [positives[i] for i in group]
        site_rows.append({
            "site_id": index + 1,
            "incident": site_incident[index],
            "min_lon": min(m["bounds"][0] for m in members), "min_lat": min(m["bounds"][1] for m in members),
            "max_lon": max(m["bounds"][2] for m in members), "max_lat": max(m["bounds"][3] for m in members),
            **{v: int(index in found[v]) for v in VARIANTS},
            "members": ";".join(f"{m['variant']}:{m['incident']}/{m['candidate']}" for m in members),
        })
    return {
        "rows": rows, "sites": site_rows, "warnings": warnings, "n_sites": len(sites),
        "n_landslide_incidents": len(landslide_incidents), "dedup_iou": dedup_iou, "site_iou": site_iou,
        "bootstrap": bootstrap,
    }


def fmt(value, kind):
    if value == "" or value is None:
        return "—"
    if kind == "pct":
        return f"{100 * value:.1f}%"
    if kind == "dpct":
        return f"{100 * value:+.1f} pp"
    if kind == "float":
        return "∞" if math.isinf(value) else f"{value:.1f}"
    if kind == "p":
        return f"{value:.3f}" if value >= 0.001 else "<0.001"
    return str(value)


def markdown(result):
    rows = result["rows"]
    best = {key: max(r[key] for r in rows) for key in ("precision", "relative_recall", "f1", "tp", "sites_found")}

    def bold(row, key, text):
        return f"**{text}**" if row[key] == best[key] else text

    lines = [
        "# Ablation results",
        "",
        f"Unique candidates use global greedy dedup at IoU ≥ {result['dedup_iou']}. Relative recall is measured "
        f"against {result['n_sites']} pooled landslide sites (landslide-labelled boxes from all variants merged at "
        f"IoU ≥ {result['site_iou']}), spanning {result['n_landslide_incidents']} incidents.",
        "",
        "## Main table",
        "",
        "| Variant | Removed | Raw cand. | Unique cand. | TP | Precision | Sites found | Rel. recall | F1 | Incidents hit | Cand./TP |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        lines.append(
            f"| {r['variant']} | {r['component_removed']} | {r['n_raw_candidates']} | {r['n_unique_candidates']} | "
            f"{bold(r, 'tp', r['tp'])} | {bold(r, 'precision', fmt(r['precision'], 'pct'))} | "
            f"{bold(r, 'sites_found', r['sites_found'])}/{result['n_sites']} | "
            f"{bold(r, 'relative_recall', fmt(r['relative_recall'], 'pct'))} | {bold(r, 'f1', fmt(r['f1'], 'pct'))} | "
            f"{r['incidents_hit']}/{result['n_landslide_incidents']} | {fmt(r['candidates_per_tp'], 'float')} |"
        )
    lines += [
        "",
        "## Component contribution (baseline − ablated)",
        "",
        "Positive deltas mean the component helps. Sites lost/gained are paired against the baseline; "
        f"the p-value is an exact McNemar test on those discordant sites. 95% CIs use a "
        f"{result['bootstrap']}-sample incident-level bootstrap.",
        "",
        "| Component | ΔPrecision | ΔRecall | ΔRecall 95% CI | Recall drop | ΔF1 | ΔF1 95% CI | Sites lost / gained | McNemar p |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows[1:]:
        recall_ci = f"[{fmt(r['delta_recall_ci_low'], 'dpct')}, {fmt(r['delta_recall_ci_high'], 'dpct')}]"
        f1_ci = f"[{fmt(r['delta_f1_ci_low'], 'dpct')}, {fmt(r['delta_f1_ci_high'], 'dpct')}]"
        lines.append(
            f"| {r['component_removed']} ({r['variant']}) | {fmt(r['delta_precision'], 'dpct')} | "
            f"{fmt(r['delta_recall'], 'dpct')} | {recall_ci} | {r['recall_drop_pct']:.1f}% | "
            f"{fmt(r['delta_f1'], 'dpct')} | {f1_ci} | "
            f"{r['sites_lost_vs_baseline']} / {r['sites_gained_vs_baseline']} | {fmt(r['mcnemar_p'], 'p')} |"
        )
    if result["warnings"]:
        lines += ["", "## Notes", ""] + [f"- {w}" for w in result["warnings"]]
    return "\n".join(lines) + "\n"


def write_csv(path, fields, rows):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "study_dir", type=Path, nargs="?", default=Path("ablation_candidates/ablations/study_01"),
        help="folder containing one subfolder per variant (default: %(default)s)",
    )
    parser.add_argument("--outdir", type=Path, help="output folder (default: <study_dir>/ablation_results)")
    parser.add_argument("--dedup-iou", type=float, default=0.5, help="candidate dedup IoU (default: %(default)s)")
    parser.add_argument("--site-iou", type=float, default=0.1, help="IoU to merge positives into one site (default: %(default)s)")
    parser.add_argument("--bootstrap", type=int, default=2000, help="incident bootstrap samples for Δ CIs (default: %(default)s)")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    for name in ("dedup_iou", "site_iou"):
        value = getattr(args, name)
        if not math.isfinite(value) or not 0 < value <= 1:
            parser.error(f"--{name.replace('_', '-')} must be in (0, 1]")
    if args.bootstrap < 0:
        parser.error("--bootstrap must be non-negative")

    result = analyze(args.study_dir, args.dedup_iou, args.site_iou, args.bootstrap, args.seed)
    outdir = args.outdir or args.study_dir / "ablation_results"
    outdir.mkdir(parents=True, exist_ok=True)
    write_csv(outdir / "ablation_table.csv", TABLE_FIELDS, result["rows"])
    site_fields = ["site_id", "incident", "min_lon", "min_lat", "max_lon", "max_lat", *VARIANTS, "members"]
    write_csv(outdir / "ablation_sites.csv", site_fields, result["sites"])
    report = markdown(result)
    (outdir / "ablation_table.md").write_text(report, encoding="utf-8")
    sys.stdout.write(report)
    print(f"\nWrote {outdir / 'ablation_table.csv'}, {outdir / 'ablation_table.md'}, {outdir / 'ablation_sites.csv'}")


if __name__ == "__main__":
    main()
