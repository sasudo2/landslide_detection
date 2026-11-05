import contextlib
import csv
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "analyze_ablation.py"
SPEC = importlib.util.spec_from_file_location("analyze_ablation", SCRIPT)
analysis = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = analysis
SPEC.loader.exec_module(analysis)


def box(x, y=28.0, size=0.01):
    return {"min_lon": x, "max_lon": x + size, "min_lat": y, "max_lat": y + size}


class AblationAnalysisTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def variant(self, name, rows, status=None, label_file="landslide_identification.csv"):
        folder = self.root / name
        folder.mkdir()
        with (folder / label_file).open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=[
                "incident", "candidate", "min_lat", "max_lat", "min_lon", "max_lon", "label", "timestamp",
            ])
            writer.writeheader()
            for incident, candidate, bounds, label in rows:
                writer.writerow({"incident": incident, "candidate": f"candidate_{candidate}",
                                 "label": label, "timestamp": "", **bounds})
        status = status or {1: ("ok", 5), 2: ("ok", 5)}
        with (folder / "candidate_status.csv").open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["incident_id", "status", "n_candidates", "error"])
            for incident, (state, count) in status.items():
                writer.writerow([incident, state, count, ""])
        (folder / "summary.json").write_text(json.dumps({"switches": analysis.EXPECTED_SWITCHES[name]}))

    def build(self):
        site_a, site_b, site_c = box(85.0), box(85.1), box(85.2)
        self.variant("baseline", [
            (1, 1, site_a, "landslide"), (1, 2, site_b, "landslide"),
            (2, 1, box(86.0), "not_landslide"), (2, 2, box(86.1), "not_landslide"),
        ], status={1: ("ok", 6), 2: ("ok", 4), 3: ("failed", 0)})
        # Not deduplicated: candidate 2 repeats candidate 1 and must inherit nothing.
        self.variant("optical_only", [
            (1, 1, site_a, "landslide"), (1, 2, site_a, "not_landslide"),
            (2, 1, site_c, "landslide"), (3, 1, box(87.0), "landslide"),
        ], status={1: ("ok", 2), 2: ("ok", 1), 3: ("ok", 1)}, label_file="landslides_identification.csv")
        for name in ("sar_only", "no_slope", "no_slic", "no_bonferroni"):
            self.variant(name, [(1, 1, site_a, "landslide"), (2, 1, box(86.0), "not_landslide")])

    def test_table_uses_unique_candidates_common_incidents_and_pooled_sites(self):
        self.build()
        result = analysis.analyze(self.root, bootstrap=50)
        rows = {row["variant"]: row for row in result["rows"]}
        self.assertEqual(result["n_sites"], 3)
        self.assertTrue(any("[3]" in warning for warning in result["warnings"]))

        base = rows["baseline"]
        self.assertEqual((base["n_unique_candidates"], base["tp"], base["sites_found"]), (4, 2, 2))
        self.assertEqual(base["n_raw_candidates"], 10)
        self.assertAlmostEqual(base["precision"], 0.5)
        self.assertAlmostEqual(base["relative_recall"], 2 / 3)

        optical = rows["optical_only"]
        self.assertEqual(optical["n_dedup_removed_here"], 1)
        self.assertEqual((optical["n_unique_candidates"], optical["tp"], optical["sites_found"]), (2, 2, 2))
        self.assertEqual((optical["sites_lost_vs_baseline"], optical["sites_gained_vs_baseline"]), (1, 1))
        self.assertAlmostEqual(optical["delta_recall"], 0.0)

        sar = rows["sar_only"]
        self.assertAlmostEqual(sar["delta_recall"], 1 / 3)
        self.assertAlmostEqual(sar["recall_drop_pct"], 50.0)
        self.assertLessEqual(sar["delta_f1_ci_low"], sar["delta_f1_ci_high"])

    def test_cli_writes_outputs(self):
        self.build()
        outdir = self.root / "out"
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            analysis.main([str(self.root), "--outdir", str(outdir), "--bootstrap", "10"])
        self.assertIn("| baseline |", stdout.getvalue())
        for name in ("ablation_table.csv", "ablation_table.md", "ablation_sites.csv"):
            self.assertTrue((outdir / name).exists())
        with (outdir / "ablation_table.csv").open() as handle:
            self.assertEqual([row["variant"] for row in csv.DictReader(handle)], list(analysis.VARIANTS))

    def test_mcnemar_exact(self):
        self.assertEqual(analysis.mcnemar_exact(0, 0), 1.0)
        self.assertAlmostEqual(analysis.mcnemar_exact(5, 0), 0.0625)


if __name__ == "__main__":
    unittest.main()
