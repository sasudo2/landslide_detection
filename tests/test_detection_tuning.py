import ast
import json
import math
import unittest
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK = ROOT / "notebooks" / "analysis" / "detection_tuning.ipynb"
BASELINE = ROOT / "notebooks" / "experiments" / "ablation" / "ablation_baseline.ipynb"


def code_cells(path):
    notebook = json.loads(path.read_text(encoding="utf-8"))
    sources = []
    for cell in notebook["cells"]:
        source = cell.get("source", "")
        source = "".join(source) if isinstance(source, list) else source
        if cell["cell_type"] == "code" and not source.startswith("%"):
            sources.append(source)
    return "\n\n".join(sources)


def top_level(source):
    tree = ast.parse(source)
    nodes = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            nodes[node.name] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    nodes[target.id] = node
    return tree, nodes


def literal(nodes, name):
    return ast.literal_eval(nodes[name].value)


def load_functions(source, nodes, names, namespace):
    module = ast.Module(body=[nodes[name] for name in names], type_ignores=[])
    exec(compile(module, str(NOTEBOOK), "exec"), namespace)
    return namespace


class DetectionTuningNotebookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = code_cells(NOTEBOOK)
        cls.tree, cls.nodes = top_level(cls.source)
        cls.baseline_source = code_cells(BASELINE)
        _, cls.baseline_nodes = top_level(cls.baseline_source)

    def test_not_picked_up_as_an_ablation_run_notebook(self):
        self.assertFalse(NOTEBOOK.name.startswith("ablation_"))

    def test_uses_the_ablation_cohort_and_pinned_raw_commit(self):
        self.assertRegex(literal(self.nodes, "RAW_COMMIT_SHA"), r"^[0-9a-f]{40}$")
        self.assertEqual(literal(self.nodes, "RAW_REPO"), literal(self.baseline_nodes, "RAW_REPO"))
        self.assertEqual(literal(self.nodes, "RAW_ROOT"), literal(self.baseline_nodes, "RAW_ROOT"))
        self.assertEqual(literal(self.nodes, "COHORT_START"), literal(self.baseline_nodes, "COHORT_START"))
        self.assertEqual(literal(self.nodes, "COHORT_STOP"), literal(self.baseline_nodes, "COHORT_STOP"))
        cohort = literal(self.nodes, "COHORT_IDS")
        self.assertEqual(len(cohort), 100)
        self.assertEqual(len(set(cohort)), 100)

    def test_ground_truth_has_66_sites_with_valid_boxes(self):
        members = literal(self.nodes, "GT_MEMBERS")
        self.assertEqual(len({row[0] for row in members}), 66)
        cohort = set(literal(self.nodes, "COHORT_IDS"))
        for site, site_incident, _variant, incident, _candidate, *box in members:
            self.assertIn(site_incident, cohort)
            self.assertIn(incident, cohort)
            self.assertLess(box[0], box[2])
            self.assertLess(box[1], box[3])

    def test_baseline_params_match_ablation_settings(self):
        params = literal(self.nodes, "BASELINE_PARAMS")
        settings = literal(self.baseline_nodes, "SETTINGS")
        fixed = literal(self.nodes, "SETTINGS")
        for key, value in params.items():
            if key in settings:
                self.assertEqual(value, settings[key], key)
        for key, value in fixed.items():
            self.assertEqual(value, settings[key], key)
        self.assertEqual(params["optical_weight"], 0.5)
        self.assertEqual(params["min_severity"], 0.0)
        self.assertEqual(params["score_mode"], "area_x_severity")

    def test_search_space_contains_every_baseline_value(self):
        params = literal(self.nodes, "BASELINE_PARAMS")
        space = literal(self.nodes, "SEARCH_SPACE")
        self.assertEqual(set(space), set(params))
        for name, spec in space.items():
            with self.subTest(name=name):
                if spec[0] == "categorical":
                    self.assertIn(params[name], spec[1])
                else:
                    self.assertLessEqual(spec[1], params[name])
                    self.assertLessEqual(params[name], spec[2])
        self.assertTrue(set(literal(self.nodes, "SEGMENT_KEYS")) <= set(space))

    def test_evidence_helpers_are_verbatim_copies(self):
        names = (
            "norm01", "wmean_cov", "cca_axes", "irmad", "build_target_grid", "reproject_bands",
            "select_rgb_band_indexes", "load_optical", "load_slope", "load_sar",
            "change_evidence", "aggregate_segments",
        )
        for name in names:
            with self.subTest(name=name):
                self.assertEqual(ast.dump(self.nodes[name]), ast.dump(self.baseline_nodes[name]))

    def test_dedup_matching_and_scores(self):
        namespace = {
            "np": np, "math": math, "DEDUP_IOU": 0.5, "MATCH_IOU": 0.2,
            "GT_BOXES": np.array([[0.0, 0.0, 1.0, 1.0], [10.0, 10.0, 11.0, 11.0]]),
            "GT_SITE_IDS": np.array([1, 2]),
            "SITE_INCIDENT": {1: 100, 2: 200},
        }
        load_functions(
            self.source, self.nodes,
            ("iou_one_to_many", "deduplicate", "match_sites", "score_candidates"), namespace,
        )

        def candidate(incident, rank, box):
            return {"incident": incident, "rank": rank, "box": box, "chip_box": box}

        raw = [
            candidate(100, 1, (0.0, 0.0, 1.0, 1.0)),       # matches site 1
            candidate(100, 2, (0.05, 0.0, 1.05, 1.0)),     # duplicate of rank 1
            candidate(100, 3, (5.0, 5.0, 6.0, 6.0)),       # false positive
            candidate(200, 1, (20.0, 20.0, 21.0, 21.0)),   # false positive
        ]
        unique = namespace["match_sites"](namespace["deduplicate"](raw))
        self.assertEqual([(c["incident"], c["rank"]) for c in unique], [(100, 1), (100, 3), (200, 1)])
        self.assertEqual([c["sites"] for c in unique], [[1], [], []])
        scores = namespace["score_candidates"](unique, [100, 200])
        self.assertEqual((scores["tp"], scores["fp"], scores["sites_found"], scores["sites_total"]), (1, 2, 1, 2))
        self.assertAlmostEqual(scores["precision"], 1 / 3)
        self.assertAlmostEqual(scores["recall"], 0.5)
        only_100 = namespace["score_candidates"](unique, [100])
        self.assertEqual((only_100["sites_total"], only_100["recall"]), (1, 1.0))

    def test_selection_prefers_fewest_false_positives_without_losing_sites(self):
        import pandas as pd

        namespace = {"math": math}
        load_functions(self.source, self.nodes, ("select_trial",), namespace)
        trials = pd.DataFrame([
            {"trial": 0, "sites_found": 10, "fp": 300, "precision": 0.03, "f1": 0.05, "f2": 0.10},
            {"trial": 1, "sites_found": 12, "fp": 250, "precision": 0.05, "f1": 0.08, "f2": 0.15},
            {"trial": 2, "sites_found": 9, "fp": 20, "precision": 0.30, "f1": 0.30, "f2": 0.20},
        ])
        select = namespace["select_trial"]
        self.assertEqual(select(trials, {"sites_found": 10}, "min_fp_at_baseline_recall")["trial"], 1)
        self.assertEqual(select(trials, {"sites_found": 10}, "max_f1")["trial"], 2)
        self.assertEqual(select(trials, {"sites_found": 99}, "min_fp_at_baseline_recall")["trial"], 2)

    def test_score_modes(self):
        namespace = {"math": math}
        load_functions(self.source, self.nodes, ("candidate_score",), namespace)
        score = namespace["candidate_score"]
        self.assertEqual(score(400.0, 0.5, "area_x_severity"), 200.0)
        self.assertEqual(score(400.0, 0.5, "sqrt_area_x_severity"), 10.0)
        self.assertEqual(score(400.0, 0.5, "severity"), 0.5)
        with self.assertRaises(ValueError):
            score(1.0, 1.0, "unknown")


if __name__ == "__main__":
    unittest.main()
