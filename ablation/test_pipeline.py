import argparse
import ast
import csv
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import rasterio
from rasterio.transform import from_origin

from ablation import pipeline as p


class DetectionTests(unittest.TestCase):
    @staticmethod
    def strong_change(before, after, optical, settings):
        _, h, w = before.shape
        row, col = np.indices((h, w))
        confidence = np.exp(-((row - h / 2) ** 2 + (col - w / 2) ** 2) / 200).astype("float32")
        df = np.full((h, w), before.shape[0], dtype="float64")
        return confidence, df + 1000 * confidence, df

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = p.Settings()
        rng = np.random.default_rng(42)
        self.optical = rng.uniform(10, 200, (3, 64, 64)).astype("float32")
        self.sar = rng.uniform(-20, -3, (2, 64, 64)).astype("float32")
        self.paths = {}
        self.transform = from_origin(85, 28, 0.0001, 0.0001)
        for key, data in {
            "before": self.optical,
            "after": self.optical + rng.normal(0, 3, self.optical.shape),
            "sar_pre": self.sar,
            "sar_post": self.sar + rng.normal(0, 0.5, self.sar.shape),
            "slope": np.full((64, 64), 35),
        }.items():
            path = self.root / f"{key}.tif"
            p.write_chip(path, data, self.transform, p.TARGET_CRS)
            self.paths[key] = str(path)

    def test_baseline_matches_notebook(self):
        notebook = json.loads((Path(__file__).parents[1] / "candidate_detection.ipynb").read_text())
        source = "".join(notebook["cells"][2]["source"])
        tree = ast.parse(source)
        namespace = dict(vars(p))
        for node in tree.body:
            if isinstance(node, ast.Assign) and all(
                isinstance(target, ast.Name) and target.id.isupper() for target in node.targets
            ):
                exec(compile(ast.Module(body=[node], type_ignores=[]), "<notebook>", "exec"), namespace)
            if isinstance(node, ast.FunctionDef) and node.name in {
                "norm01", "wmean_cov", "cca_axes", "irmad", "build_target_grid",
                "reproject_bands", "_looks_like_alpha_band", "select_rgb_band_indexes",
                "load_optical", "load_slope", "load_sar",
            }:
                exec(compile(ast.Module(body=[node], type_ignores=[]), "<notebook>", "exec"), namespace)
        process = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "process_incident")
        stop = next(
            i for i, node in enumerate(process.body)
            if isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "pixel_size_m"
        )
        process.body = process.body[:stop] + ast.parse("return {'mask': mask, 'candidates': cand}").body
        module = ast.fix_missing_locations(ast.Module(body=[process], type_ignores=[]))
        suffixes = {
            "after": "after", "slope": "slope", "planet_before": "before",
            "sar_pre": "sar_pre", "sar_post": "sar_post",
        }
        namespace["hf_fetch"] = lambda token, incident_id, name: self.paths[
            suffixes[name.removeprefix(f"incident_{incident_id}_").removesuffix(".tif")]
        ]
        namespace["hf_token"] = None
        exec(compile(module, "<notebook>", "exec"), namespace)
        expected = namespace["process_incident"](1)
        actual = p.detect(self.paths, p.VARIANTS["baseline"], self.settings)
        np.testing.assert_array_equal(actual["mask"], expected["mask"])
        self.assertEqual(len(actual["candidates"]), len(expected["candidates"]))
        for a, b in zip(actual["candidates"], expected["candidates"]):
            for key in ("area_m2", "elongation", "severity", "score", "bbox_px"):
                np.testing.assert_allclose(a[key], b[key])

    def test_disabled_modality_never_enters_detection(self):
        for name, forbidden in (("sar_only", {"before", "after"}), ("optical_only", {"sar_pre", "sar_post"})):
            original = p.reproject_bands

            def checked(path, *args):
                self.assertNotIn(path, [self.paths[key] for key in forbidden])
                return original(path, *args)

            with self.subTest(name=name), patch.object(p, "reproject_bands", side_effect=checked):
                result = p.detect(self.paths, p.VARIANTS[name], self.settings)
                self.assertTrue(np.all(np.isfinite(result["mask"])))

    def test_no_slope_removes_all_slope_influence(self):
        first = p.detect(self.paths, p.VARIANTS["no_slope"], self.settings)
        p.write_chip(self.paths["slope"], np.zeros((64, 64)), self.transform, p.TARGET_CRS)
        second = p.detect(self.paths, p.VARIANTS["no_slope"], self.settings)
        np.testing.assert_array_equal(first["mask"], second["mask"])
        self.assertEqual(first["candidates"], second["candidates"])
        baseline = p.detect(self.paths, p.VARIANTS["baseline"], self.settings)
        self.assertFalse(baseline["mask"].any())

    def test_segmentation_features_exclude_disabled_inputs(self):
        shape = (12, 12)
        conf, slope = np.ones(shape), np.arange(144).reshape(shape)
        post = np.ones((2, *shape))
        self.assertEqual(p.segmentation_features(conf, post, slope, p.VARIANTS["sar_only"]).shape[-1], 4)
        no_slope = p.segmentation_features(conf, post, slope, p.VARIANTS["no_slope"])
        other = p.segmentation_features(conf, post, slope * 4 + 30, p.VARIANTS["no_slope"])
        np.testing.assert_array_equal(no_slope, other)
        self.assertEqual(no_slope.shape[-1], 3)

    def test_no_slic_is_pixelwise_and_bonferroni_is_only_alpha_change(self):
        shape = (30, 30)
        confidence = np.arange(900, dtype=np.float32).reshape(shape) / 900
        terrain = np.ones(shape, dtype=bool)
        df = np.full(shape, 5.0)
        chi = df + 2.5 * np.sqrt(2 * df)
        settings = replace(self.settings, high_percentile=80, low_percentile=70)
        with patch.object(p, "slic", side_effect=AssertionError("SLIC must not run")):
            corrected, n_tests, critical = p.detection_mask(
                confidence, chi, df, terrain, None, 100, p.VARIANTS["no_slic"], settings,
            )
            uncorrected, _, uncorrected_critical = p.detection_mask(
                confidence, chi, df, terrain, None, 100,
                replace(p.VARIANTS["no_slic"], bonferroni=False), settings,
            )
        self.assertEqual(n_tests, 900)
        self.assertAlmostEqual(critical, p.norm.ppf(1 - 0.05 / 900))
        self.assertAlmostEqual(uncorrected_critical, p.norm.ppf(0.95))
        self.assertFalse(corrected.any())
        self.assertTrue(uncorrected.any())
        weak_chi = df + np.sqrt(2 * df)
        weak_mask, _, _ = p.detection_mask(
            confidence, weak_chi, df, terrain, None, 100,
            replace(p.VARIANTS["no_slic"], bonferroni=False), settings,
        )
        self.assertFalse(weak_mask.any(), "Uncorrected mode must retain the significance gate")

    def test_aggregation_matches_notebook_loop(self):
        rng = np.random.default_rng(3)
        segments = np.repeat(np.arange(5), 60).reshape(15, 20)
        terrain = rng.random(segments.shape) > 0.2
        confidence, chi, df = [rng.random(segments.shape) for _ in range(3)]
        actual_conf, actual_z, count = p.aggregate_segments(confidence, chi, df, terrain, segments)
        self.assertEqual(count, 5)
        for sid in range(5):
            active = (segments == sid) & terrain
            expected_z = (chi[active].sum() - df[active].sum()) / np.sqrt(2 * df[active].sum())
            np.testing.assert_allclose(actual_conf[segments == sid], confidence[active].mean(), rtol=1e-6)
            np.testing.assert_allclose(actual_z[segments == sid], expected_z, rtol=1e-6)

    def test_sparse_selected_modality_fails_instead_of_falling_back(self):
        with self.assertRaisesRegex(ValueError, "<=100"):
            p.change_evidence(np.zeros((2, 20, 20)), np.zeros((2, 20, 20)), False, self.settings)
        # Negative dB data must remain valid.
        confidence, _, df = p.change_evidence(self.sar, self.sar + 0.3, False, self.settings)
        self.assertTrue(np.all(df == 2))
        self.assertTrue(np.isfinite(confidence).all())

    def test_candidate_chip_layout_and_native_grid(self):
        result = {
            "mask": np.ones((64, 64), dtype=bool), "slope": np.full((64, 64), 35),
            "transform": self.transform, "px_area": 100,
            "candidates": [{
                "area_m2": 6400, "elongation": 1, "severity": 0.9, "score": 5760,
                "lon_min": 85, "lat_min": 27.996, "lon_max": 85.004, "lat_max": 28,
                "bbox_px": [20, 20, 28, 28],
            }],
        }
        rows = p.export_candidates(8, self.paths, result, self.root / "incident_8", self.settings)
        self.assertEqual(rows[0]["candidate_id"], 1)
        self.assertNotIn("bbox_px", rows[0])
        chips = list(self.root.glob("incident_8/candidate_1/*.tif"))
        self.assertEqual(len(chips), 4)
        for chip in chips:
            with rasterio.open(chip) as src:
                self.assertEqual(str(src.crs), p.TARGET_CRS)
                self.assertTrue(np.isfinite(src.read()).all())
                if chip.stem.endswith(("before", "after")):
                    self.assertEqual(src.count, 3)
                    self.assertEqual(src.transform.a, self.transform.a)
                else:
                    self.assertEqual(src.count, 1)

    def test_six_run_cli_writes_comparison_and_uses_isolated_remote_paths(self):
        api = Mock()
        raw_files = {
            f"{p.RAW_ROOT}/incident_8/incident_8_{suffix}.tif"
            for suffix in ("planet_before", "after", "slope", "sar_pre", "sar_post")
        }
        api.list_repo_files.side_effect = lambda repo, **kwargs: raw_files if kwargs["revision"] == "raw-sha" else []
        api.repo_info.return_value = SimpleNamespace(sha="raw-sha")
        argv = [
            "pipeline", "--study-root", "ablations/integration", "--output-dir", str(self.root / "results"),
            "--incident-ids", "8",
        ]
        with patch.object(p, "HfApi", return_value=api), patch.object(
            p, "fetch_paths", return_value=self.paths,
        ), patch.object(
            p, "change_evidence", side_effect=self.strong_change,
        ), patch.object(sys, "argv", argv):
            p.main()
        study_folder = self.root / "results/ablations/integration"
        with (study_folder / "comparison.csv").open() as handle:
            comparison = list(csv.DictReader(handle))
        self.assertEqual([row["variant"] for row in comparison], list(p.VARIANTS))
        self.assertTrue(all(row["n_failed"] == "0" for row in comparison))
        self.assertTrue(all(int(row["n_candidates"]) > 0 for row in comparison))
        for name in p.VARIANTS:
            run = json.loads((study_folder / name / "run.json").read_text())
            self.assertEqual(run["study"]["raw_revision"], "raw-sha")
            self.assertEqual(run["study"]["incident_ids"], [8])
            self.assertIn("numpy", run["study"]["package_versions"])
        for call in api.upload_folder.call_args_list:
            self.assertTrue(call.kwargs["path_in_repo"].startswith("ablations/integration/"))
        self.assertEqual(api.create_commit.call_count, 6)
        for call in api.create_commit.call_args_list:
            ops = call.kwargs["operations"]
            self.assertGreaterEqual(len(ops), 4)
            for op in ops:
                self.assertRegex(op.path_in_repo, r"^ablations/integration/\w+/incident_8/candidate_\d+/.+\.tif$")
                self.assertTrue(Path(op.path_or_fileobj).is_file())
        with patch.object(p, "HfApi", return_value=api), patch.object(sys, "argv", argv):
            with self.assertRaises(FileExistsError):
                p.main()

    def test_actual_detection_produces_ranked_nonempty_candidates(self):
        with patch.object(p, "change_evidence", side_effect=self.strong_change):
            for name in p.VARIANTS:
                with self.subTest(name=name):
                    result = p.detect(self.paths, p.VARIANTS[name], self.settings)
                    self.assertTrue(result["mask"].any())
                    self.assertGreater(len(result["candidates"]), 0)
                    scores = [candidate["score"] for candidate in result["candidates"]]
                    self.assertEqual(scores, sorted(scores, reverse=True))
                    self.assertGreaterEqual(result["candidates"][0]["area_m2"], 3000)


class StudyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.args = argparse.Namespace(
            repo="owner/raw", output_repo="owner/results", raw_revision="main",
            output_revision="main", study_root="ablations/test", output_dir=self.root,
            incident_ids=None, limit=None, range=None, workers=4,
        )
        self.files = {
            f"{p.RAW_ROOT}/incident_{i}/incident_{i}_{suffix}.tif"
            for i in (2, 10)
            for suffix in ("planet_before", "after", "slope", "sar_pre", "sar_post")
        }
        self.api = Mock()
        self.api.list_repo_files.side_effect = lambda repo, **kwargs: self.files if repo == "owner/raw" else []
        self.api.repo_info.return_value = SimpleNamespace(sha="raw-commit")

    def test_common_cohort_and_pinned_revision_reused(self):
        settings = p.Settings()
        study, _, _, local = p.prepare_study(self.api, self.args, settings)
        self.assertEqual(study["incident_ids"], [2, 10])
        self.assertEqual(study["raw_revision"], "raw-commit")
        self.api.repo_info.reset_mock()
        manifest = local / "study.json"
        self.api.list_repo_files.side_effect = lambda repo, **kwargs: (
            self.files if repo == "owner/raw" else ["ablations/test/study.json"]
        )
        with patch.object(p, "hf_hub_download", return_value=str(manifest)):
            reused, _, _, _ = p.prepare_study(self.api, self.args, settings)
        self.assertEqual(reused, study)
        self.api.repo_info.assert_not_called()
        self.api.list_repo_files.assert_any_call("owner/raw", repo_type="dataset", revision="raw-commit")

    def test_range_selects_positions_among_complete_incidents(self):
        self.files = {
            f"{p.RAW_ROOT}/incident_{i}/incident_{i}_{suffix}.tif"
            for i in (50, 70, 20, 90, 40)
            for suffix in ("planet_before", "after", "slope", "sar_pre", "sar_post")
        } | {f"{p.RAW_ROOT}/incident_30/incident_30_after.tif"}
        self.args.range = [2, 4]
        study, _, _, _ = p.prepare_study(self.api, self.args, p.Settings())
        self.assertEqual(study["incident_ids"], [40, 50, 70])
        self.assertEqual(study["range"], [2, 4])
        self.args.range = [2, 6]
        self.args.study_root = "ablations/other"
        with self.assertRaisesRegex(ValueError, "exceeds the 5 complete"):
            p.prepare_study(self.api, self.args, p.Settings())

    def test_incomplete_cohort_or_missing_requested_ids_raise(self):
        self.assertEqual(p.complete_incidents(self.files - {
            f"{p.RAW_ROOT}/incident_2/incident_2_sar_post.tif",
        }), [10])
        self.args.incident_ids = [99]
        with self.assertRaisesRegex(ValueError, "lack complete inputs"):
            p.prepare_study(self.api, self.args, p.Settings())

    def test_root_cannot_touch_production_folders(self):
        for root in ("", ".", "../candidates", "/candidates", "candidates/test", "raw_images/test", "a//b", "a/../b"):
            with self.subTest(root=root), self.assertRaises(argparse.ArgumentTypeError):
                p.validate_study_root(root)
        self.assertEqual(p.validate_study_root("ablations/test"), "ablations/test")

    def test_zero_candidate_runs_upload_header_only_summaries(self):
        study = {"incident_ids": [2], "raw_revision": "raw-commit"}
        result = {"n_tests": 50, "z_crit": 3.09, "mask": np.zeros((10, 10), dtype=bool)}
        with patch.object(p, "fetch_paths", return_value={}), patch.object(p, "detect", return_value=result), patch.object(
            p, "export_candidates", return_value=[],
        ):
            summary = p.run_variant(self.api, self.args, study, self.files, self.root, "sar_only", p.Settings())
        self.assertEqual(summary["n_candidates"], 0)
        self.assertEqual(summary["n_ok"], 1)
        with (self.root / "sar_only/candidate_metadata.csv").open() as handle:
            self.assertEqual(next(csv.reader(handle)), p.META_FIELDS)
            self.assertEqual(handle.read(), "")
        self.api.upload_file.assert_called_once()
        self.api.upload_folder.assert_called_once()
        self.assertEqual(self.api.upload_folder.call_args.kwargs["path_in_repo"], "ablations/test/sar_only")
        self.api.create_commit.assert_not_called()

    def _fake_process(self, delays, upload_fail=False):
        import threading
        import time

        active, peak, lock = [0], [0], threading.Lock()

        def detect(paths, variant, settings):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(delays[paths["id"]])
            with lock:
                active[0] -= 1
            return {"n_tests": 1, "z_crit": 2.0, "mask": np.zeros((2, 2), dtype=bool)}

        def export(incident_id, paths, result, folder, settings):
            chip = folder / "candidate_1" / "x.tif"
            chip.parent.mkdir()
            chip.write_bytes(b"x")
            return [{"incident_id": incident_id, "candidate_id": 1, **{k: 0 for k in p.META_FIELDS[2:]}}]

        if upload_fail:
            self.api.create_commit.side_effect = OSError("hub down")
        patches = (
            patch.object(p, "fetch_paths", side_effect=lambda repo, rev, i, files, token: {"id": i}),
            patch.object(p, "detect", side_effect=detect),
            patch.object(p, "export_candidates", side_effect=export),
        )
        return patches, peak

    def test_parallel_workers_keep_outputs_sorted_and_batch_uploads(self):
        ids = [5, 1, 9, 3, 7, 2]
        # Later IDs finish first so completion order differs from ID order.
        patches, peak = self._fake_process({i: 0.3 / i for i in ids})
        with patches[0], patches[1], patches[2], patch.object(p, "UPLOAD_BATCH_FILES", 2):
            summary = p.run_variant(
                self.api, self.args, {"incident_ids": ids, "raw_revision": "sha"},
                self.files, self.root, "baseline", p.Settings(),
            )
        self.assertGreater(peak[0], 1)
        self.assertEqual(summary["n_candidates"], 6)
        for name in ("candidate_status.csv", "candidate_metadata.csv"):
            with (self.root / "baseline" / name).open() as handle:
                self.assertEqual([int(r["incident_id"]) for r in csv.DictReader(handle)], sorted(ids))
        diagnostics = json.loads((self.root / "baseline/diagnostics.json").read_text())
        self.assertEqual([d["incident_id"] for d in diagnostics], sorted(ids))
        uploaded = [op.path_in_repo for c in self.api.create_commit.call_args_list for op in c.kwargs["operations"]]
        self.assertEqual(len(uploaded), 6)
        self.assertEqual(self.api.create_commit.call_count, 3)

    def test_upload_failure_marks_incidents_failed(self):
        patches, _ = self._fake_process({1: 0, 2: 0}, upload_fail=True)
        with patches[0], patches[1], patches[2], self.assertLogs(p.LOG, level="ERROR"):
            summary = p.run_variant(
                self.api, self.args, {"incident_ids": [1, 2], "raw_revision": "sha"},
                self.files, self.root, "baseline", p.Settings(),
            )
        self.assertEqual(summary["n_failed"], 2)
        self.assertEqual(summary["n_candidates"], 0)
        with (self.root / "baseline/candidate_metadata.csv").open() as handle:
            self.assertEqual(list(csv.DictReader(handle)), [])
        with (self.root / "baseline/candidate_status.csv").open() as handle:
            self.assertTrue(all("hub down" in r["error"] for r in csv.DictReader(handle)))

    def test_failures_are_reported_not_counted_as_success(self):
        with patch.object(p, "fetch_paths", side_effect=OSError("test read error")), self.assertLogs(p.LOG, level="ERROR"):
            summary = p.run_variant(
                self.api, self.args, {"incident_ids": [2], "raw_revision": "sha"},
                self.files, self.root, "baseline", p.Settings(),
            )
        self.assertEqual(summary["n_failed"], 1)
        self.assertEqual(summary["n_ok"], 0)
        with (self.root / "baseline/candidate_status.csv").open() as handle:
            row = next(csv.DictReader(handle))
        self.assertEqual(row["status"], "failed")
        self.assertIn("test read error", row["error"])

    def test_existing_local_variant_is_not_overwritten(self):
        (self.root / "baseline").mkdir()
        with self.assertRaises(FileExistsError):
            p.run_variant(self.api, self.args, {}, self.files, self.root, "baseline", p.Settings())
        self.api.upload_file.assert_not_called()


class TokenTests(unittest.TestCase):
    def test_env_token_wins_then_kaggle_secret(self):
        with patch.dict("os.environ", {"HF_TOKEN": "env"}):
            self.assertEqual(p.resolve_hf_token(), "env")
        client = Mock()
        client.return_value.get_secret.return_value = "kaggle"
        module = SimpleNamespace(UserSecretsClient=client)
        with patch.dict("os.environ", {}, clear=True), patch.dict(sys.modules, {"kaggle_secrets": module}):
            self.assertEqual(p.resolve_hf_token(), "kaggle")
        client.return_value.get_secret.assert_called_once_with("huggingface_token")
        with patch.dict("os.environ", {}, clear=True), patch.dict(sys.modules, {"kaggle_secrets": None}):
            self.assertIsNone(p.resolve_hf_token())


if __name__ == "__main__":
    unittest.main()
