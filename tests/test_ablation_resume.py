import ast
import contextlib
import csv
import io
import json
import re
import shutil
import tempfile
import traceback
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from test_ablation_revisions import RUN_NOTEBOOKS


PREFIX = "ablations/study_01/optical_only/"


def sources(path):
    return [
        "".join(cell["source"]) if isinstance(cell["source"], list) else cell["source"]
        for cell in json.loads(path.read_text())["cells"]
    ]


def helpers(path):
    tree = ast.parse(sources(path)[6])
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    namespace = {"Path": Path, "json": json, "csv": csv, "re": re}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


def chip_files(incident_id, candidate_id=1):
    return {
        f"{PREFIX}incident_{incident_id}/candidate_{candidate_id}/"
        f"incident_{incident_id}_candidate_{candidate_id}_{kind}.tif"
        for kind in ("before", "after", "slope", "mask")
    }


class AblationResumeTests(unittest.TestCase):
    def test_exact_coverage_threshold_reports_modality_without_fitting_sparse_data(self):
        import numpy as np

        for notebook in RUN_NOTEBOOKS:
            tree = ast.parse(sources(notebook)[5])
            nodes = [
                node for node in tree.body
                if (isinstance(node, ast.ClassDef) and node.name == "InsufficientCoverageError")
                or (isinstance(node, ast.FunctionDef) and node.name == "change_evidence")
            ]
            for optical in (True, False):
                for n_valid in (0, 100, 101):
                    with self.subTest(notebook=notebook.name, optical=optical, n_valid=n_valid):
                        fit = Mock(return_value=(np.ones(n_valid), np.ones(n_valid)))
                        namespace = {"np": np, "irmad": fit}
                        exec(compile(ast.Module(body=nodes, type_ignores=[]),
                                     str(notebook), "exec"), namespace)
                        data = np.zeros((2, 1, 150))
                        data[:, :, :n_valid] = 1.0 if optical else -10.0
                        if n_valid <= 100:
                            modality = "Optical" if optical else "SAR"
                            with self.assertRaisesRegex(
                                namespace["InsufficientCoverageError"],
                                f"{modality} has {n_valid} valid paired pixels",
                            ):
                                namespace["change_evidence"](data, data, optical)
                            fit.assert_not_called()
                        else:
                            _, _, _, valid = namespace["change_evidence"](data, data, optical)
                            self.assertEqual(int(valid.sum()), 101)
                            fit.assert_called_once()

    def test_partial_comparison_accepts_explicitly_incomplete_legacy_accounting(self):
        root = RUN_NOTEBOOKS[0].parent
        tree = ast.parse(sources(root / "ablation_compare.ipynb")[3])
        derive = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "derive_counts"
        )
        namespace = {}
        exec(compile(ast.Module(body=[derive], type_ignores=[]), "comparison", "exec"), namespace)
        summary = {
            "variant": "optical_only", "n_incidents": 1, "n_ok": 1, "n_failed": 0,
            "n_candidates": 1, "n_incidents_with_candidates": 1,
            "complete": False, "metadata_complete": False,
        }
        counts, complete, errors = namespace["derive_counts"](
            {"incident_ids": [1]}, summary,
            [{"incident_id": "1", "status": "ok", "n_candidates": "1"}],
            "optical_only", allow_partial=True,
        )
        self.assertFalse(complete)
        self.assertFalse(errors)
        self.assertEqual(counts["n_candidates"], 1)

    def test_existing_folder_preflight_pins_original_commit_and_schedules_only_missing(self):
        for notebook in RUN_NOTEBOOKS:
            with self.subTest(notebook=notebook.name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                previous = {"raw_repo": "owner/raw", "raw_commit_sha": "a" * 40}
                manifest = root / "study.json"
                manifest.write_text(json.dumps(previous))
                ids = list(range(100, 200))

                class Frame:
                    columns = ["id"]
                    iloc = None

                    def __len__(self):
                        return 1000 if self.iloc is not None else 100

                    def __getitem__(self, key):
                        return SimpleNamespace(tolist=lambda: ids)

                frame = Frame()
                selected = Frame()
                frame.iloc = type("ILoc", (), {"__getitem__": lambda self, key: selected})()
                cohort = frame
                namespace = helpers(notebook)
                api = Mock()
                api.repo_info.return_value = SimpleNamespace(sha="b" * 40)
                resolver = Mock(return_value="a" * 40)
                namespace.update({
                    "PurePosixPath": __import__("pathlib").PurePosixPath,
                    "VARIANT_NAME": "optical_only",
                    "ABLATION_SWITCHES": {"modality": "optical", "slope": True,
                                         "slic": True, "bonferroni": True},
                    "WORKERS": 2, "COHORT_START": 900, "COHORT_STOP": 1000,
                    "STUDY_ROOT": "ablations/study_01", "SOURCE_CSV_NAME": "source.csv",
                    "RAW_REPO": "owner/raw", "RAW_COMMIT_SHA": "main", "RAW_ROOT": "raw",
                    "OUTPUT_REPO": "owner/output", "OUTPUT_REVISION": "main",
                    "API": api, "pd": SimpleNamespace(read_csv=lambda path: cohort),
                    "tempfile": tempfile, "utc_now": lambda: "2026-11-04",
                    "locate_source_csv": lambda: root / "source.csv",
                    "read_output_file": lambda filename: manifest,
                    "retry_hub": lambda action, description: action(),
                    "repo_files": Mock(return_value={PREFIX + "study.json"} | chip_files(100)),
                    "resolve_immutable_sha": resolver, "raw_suffixes_by_incident": Mock(),
                    "verify_cohort_inputs": Mock(return_value={}),
                    "package_versions": Mock(return_value={}),
                    "build_study": Mock(return_value=dict(previous)),
                    "load_resume_state": Mock(return_value=(
                        {100: {"incident_id": 100, "status": "ok", "n_candidates": 1}},
                        {}, {}, set(),
                    )),
                    "CommitOperationAdd": lambda **kwargs: kwargs,
                })
                with contextlib.redirect_stdout(io.StringIO()):
                    exec(compile(sources(notebook)[7], str(notebook), "exec"), namespace)
                resolver.assert_called_once_with("owner/raw", "a" * 40)
                self.assertEqual(namespace["remaining_ids"], ids[1:])
                self.assertEqual(namespace["study"], previous)
                self.assertEqual(namespace["RUN"]["n_resumed"], 1)
                api.create_commit.assert_not_called()
                self.assertEqual(namespace["LOCAL_RAW_ROOT"], namespace["LOCAL_ROOT"] / "raw_downloads")
                local_root = namespace["LOCAL_ROOT"]
                shutil.rmtree(local_root)

    def test_existing_candidates_and_zero_results_skip_missing_incidents_retry(self):
        for notebook in RUN_NOTEBOOKS:
            with self.subTest(notebook=notebook.name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                remote = {}

                def save(name, value, csv_fields=None):
                    path = root / str(len(remote))
                    if csv_fields:
                        with path.open("w", newline="") as handle:
                            writer = csv.DictWriter(handle, fieldnames=csv_fields)
                            writer.writeheader()
                            writer.writerows(value)
                    else:
                        path.write_text(json.dumps(value))
                    remote[PREFIX + name] = path

                save("candidate_status.csv", [
                    {"incident_id": 1, "status": "ok", "n_candidates": 1},
                    {"incident_id": 2, "status": "ok", "n_candidates": 0},
                    {"incident_id": 3, "status": "failed", "n_candidates": 0},
                ], ["incident_id", "status", "n_candidates"])
                save("candidate_metadata.csv", [{"incident_id": 1, "candidate_id": 1}],
                     ["incident_id", "candidate_id"])
                namespace = helpers(notebook)
                namespace["read_output_file"] = lambda filename: remote[filename]
                with contextlib.redirect_stdout(io.StringIO()):
                    records, rows, diagnostics, incomplete = namespace["load_resume_state"](
                        set(remote) | chip_files(1), PREFIX, [1, 2, 3, 4]
                    )
                self.assertEqual(set(records), {1, 2})
                self.assertEqual(records[1]["n_candidates"], 1)
                self.assertEqual(records[2]["n_candidates"], 0)
                self.assertEqual(len(rows[1]), 1)
                self.assertFalse(diagnostics[1]["diagnostics_available"])
                self.assertFalse(incomplete)

    def test_checkpoint_restores_accounting_after_interruption(self):
        for notebook in RUN_NOTEBOOKS:
            with self.subTest(notebook=notebook.name), tempfile.TemporaryDirectory() as directory:
                checkpoint = Path(directory) / "result.json"
                saved = {
                    "record": {"incident_id": 1, "status": "ok", "n_candidates": 1},
                    "rows": [{"incident_id": 1, "candidate_id": 1, "area_m2": 3500}],
                    "diagnostic": {"incident_id": 1, "n_candidates": 1, "n_tests": 50},
                }
                checkpoint.write_text(json.dumps(saved))
                namespace = helpers(notebook)
                namespace["read_output_file"] = lambda filename: checkpoint
                files = chip_files(1) | {PREFIX + "incident_1/result.json"}
                with contextlib.redirect_stdout(io.StringIO()):
                    records, rows, diagnostics, incomplete = namespace["load_resume_state"](
                        files, PREFIX, [1, 2]
                    )
                self.assertEqual(set(records), {1})
                self.assertEqual(rows[1], saved["rows"])
                self.assertEqual(diagnostics[1], saved["diagnostic"])
                self.assertFalse(incomplete)

    def test_partial_or_unaccounted_candidates_are_preserved_with_warning(self):
        for notebook in RUN_NOTEBOOKS:
            with self.subTest(notebook=notebook.name):
                namespace = helpers(notebook)
                files = {next(iter(chip_files(1)))}
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    records, rows, _, incomplete = namespace["load_resume_state"](
                        files, PREFIX, [1, 2]
                    )
                self.assertEqual(set(records), {1})
                self.assertEqual(rows[1], [])
                self.assertEqual(incomplete, {1})
                self.assertIn("WARNING", output.getvalue())

    def test_incompatible_studies_rejected_not_silently_combined(self):
        for notebook in RUN_NOTEBOOKS:
            with self.subTest(notebook=notebook.name):
                namespace = helpers(notebook)
                current = {"raw_commit_sha": "a" * 40, "settings": {"alpha": 0.05},
                           "created_at": "new", "run_environment": {"machine": "new"}}
                previous = dict(current, created_at="old", run_environment={"machine": "old"})
                namespace["validate_resume_study"](previous, current)
                previous["raw_commit_sha"] = "b" * 40
                with self.assertRaisesRegex(ValueError, "raw_commit_sha"):
                    namespace["validate_resume_study"](previous, current)

    def test_processing_cell_only_runs_missing_ids_and_checkpoints_zero_detections(self):
        for notebook, mode in (
            (notebook, mode) for notebook in RUN_NOTEBOOKS
            for mode in ("mixed", "all_done", "legacy_incomplete", "upload_failure",
                         "insufficient_coverage", "processing_failure")
        ):
            with self.subTest(notebook=notebook.name, mode=mode), tempfile.TemporaryDirectory() as directory:
                namespace = helpers(notebook)
                api = Mock()
                commits = []

                def operation(**kwargs):
                    return kwargs

                def commit(**kwargs):
                    self.assertEqual(
                        {call.args[0] for call in fetch.call_args_list},
                        set() if mode == "all_done" else {2, 3},
                    )
                    self.assertTrue((namespace["LOCAL_VARIANT"] / "summary.json").is_file())
                    self.assertFalse(any(namespace["LOCAL_RAW_ROOT"].glob("incident_*")))
                    commits.append({
                        op["path_in_repo"]: Path(op["path_or_fileobj"]).read_bytes()
                        for op in kwargs["operations"]
                    })

                api.create_commit.side_effect = commit
                if mode == "upload_failure":
                    api.create_commit.side_effect = RuntimeError("upload unavailable")
                def fetch_raw(incident_id, before):
                    raw = namespace["LOCAL_RAW_ROOT"] / f"incident_{incident_id}"
                    raw.mkdir(parents=True)
                    (raw / "raw.tif").write_bytes(b"raw")
                    return incident_id

                fetch = Mock(side_effect=fetch_raw)
                coverage_tree = ast.parse(sources(notebook)[5])
                coverage_class = [
                    node for node in coverage_tree.body
                    if isinstance(node, ast.ClassDef) and node.name == "InsufficientCoverageError"
                ]
                if coverage_class:
                    exec(compile(ast.Module(body=coverage_class, type_ignores=[]),
                                 str(notebook), "exec"), namespace)

                def detect(incident_id):
                    if incident_id == 3 and mode == "insufficient_coverage":
                        raise namespace["InsufficientCoverageError"](
                            "SAR has 42 valid paired pixels; IR-MAD requires more than 100"
                        )
                    if incident_id == 3 and mode == "processing_failure":
                        raise ValueError("Unexpected numerical error")
                    return {"n_tests": 10, "z_crit": 2, "mask": []}

                def export(incident_id, paths, result, folder):
                    self.assertTrue(
                        (namespace["LOCAL_RAW_ROOT"] / f"incident_{incident_id}" / "raw.tif").is_file()
                    )
                    if incident_id == 3:
                        return []
                    candidate = folder / "candidate_1"
                    candidate.mkdir()
                    for kind in ("before", "after", "slope", "mask"):
                        (candidate / f"incident_{incident_id}_candidate_1_{kind}.tif").write_bytes(b"chip")
                    return [{"incident_id": incident_id, "candidate_id": 1}]

                resumed = {
                    1: {"incident_id": 1, "status": "ok", "n_candidates": 1, "error": ""},
                }
                resumed_rows = {1: [{"incident_id": 1, "candidate_id": 1}]}
                if mode == "all_done":
                    resumed[2] = {"incident_id": 2, "status": "ok", "n_candidates": 1, "error": ""}
                    resumed[3] = {"incident_id": 3, "status": "ok", "n_candidates": 0, "error": ""}
                    resumed_rows[2] = [{"incident_id": 2, "candidate_id": 1}]
                namespace.update({
                    "LOCAL_VARIANT": Path(directory) / "staged",
                    "LOCAL_RAW_ROOT": Path(directory) / "raw_downloads",
                    "resume_records": resumed,
                    "resume_rows": resumed_rows,
                    "resume_diagnostics": {1: {"incident_id": 1, "n_candidates": 1}},
                    "incomplete_incidents": {1} if mode == "legacy_incomplete" else set(),
                    "remaining_ids": [] if mode == "all_done" else [2, 3],
                    "incident_ids": [1, 2, 3], "WORKERS": 2,
                    "VARIANT_NAME": "optical_only", "ABLATION_SWITCHES": {},
                    "study_root": "ablations/study_01", "OUTPUT_REPO": "owner/output",
                    "OUTPUT_REVISION": "main", "API": api, "CommitOperationAdd": operation,
                    "retry_hub": lambda action, description: action(), "shutil": shutil,
                    "ThreadPoolExecutor": ThreadPoolExecutor, "as_completed": as_completed,
                    "traceback": traceback, "before_by_incident": {},
                    "fetch_paths": fetch,
                    "detect": detect,
                    "export_candidates": export,
                    "np": type("NP", (), {"count_nonzero": staticmethod(lambda mask: 0)}),
                    "STATUS_FIELDS": ["incident_id", "status", "n_candidates", "error"],
                    "META_FIELDS": ["incident_id", "candidate_id"],
                    "study": {"raw_commit_sha": "a" * 40}, "RUN": {},
                    "utc_now": lambda: "2026-11-04",
                })
                output = io.StringIO()
                namespace["LOCAL_VARIANT"].mkdir()
                namespace["write_json"](namespace["LOCAL_VARIANT"] / "study.json", namespace["study"])
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                    if mode == "upload_failure":
                        with self.assertRaisesRegex(RuntimeError, "Final commit failed") as raised:
                            exec(compile(sources(notebook)[8], str(notebook), "exec"), namespace)
                    elif mode == "processing_failure":
                        with self.assertRaisesRegex(RuntimeError, "Unexpected numerical error"):
                            exec(compile(sources(notebook)[8], str(notebook), "exec"), namespace)
                    else:
                        exec(compile(sources(notebook)[8], str(notebook), "exec"), namespace)
                if mode in ("insufficient_coverage", "processing_failure"):
                    self.assertFalse(any(namespace["LOCAL_RAW_ROOT"].glob("incident_*")))
                    self.assertEqual(namespace["records"][3]["status"], "failed")
                    self.assertEqual(namespace["summary"]["n_ok"], 2)
                    self.assertEqual(namespace["summary"]["n_failed"], 1)
                    self.assertEqual(namespace["summary"]["n_candidates"], 2)
                    self.assertFalse(namespace["summary"]["complete"])
                    self.assertEqual(namespace["records"][1]["status"], "ok")
                    self.assertEqual(namespace["records"][2]["status"], "ok")
                    api.create_commit.assert_called_once()
                    api.upload_folder.assert_not_called()
                    if mode == "insufficient_coverage":
                        self.assertEqual(namespace["RUN"]["status"], "incomplete")
                        self.assertEqual(namespace["summary"]["insufficient_coverage_incident_ids"], [3])
                        self.assertIn("WARNING: study incomplete", output.getvalue())
                        self.assertIn("SAR has 42 valid paired pixels", output.getvalue())
                    continue
                if mode == "upload_failure":
                    self.assertEqual(namespace["records"][1]["status"], "ok")
                    self.assertEqual(namespace["records"][2]["status"], "ok")
                    self.assertEqual(namespace["records"][3]["status"], "ok")
                    self.assertEqual(namespace["summary"]["n_candidates"], 2)
                    self.assertTrue((namespace["LOCAL_VARIANT"] / "incident_2" / "result.json").is_file())
                    self.assertTrue((namespace["LOCAL_VARIANT"] / "summary.json").is_file())
                    self.assertIn(str(namespace["LOCAL_VARIANT"]), str(raised.exception))
                    self.assertIn("retained", str(raised.exception))
                    api.upload_folder.assert_not_called()
                    continue
                self.assertEqual({call.args[0] for call in fetch.call_args_list},
                                 set() if mode == "all_done" else {2, 3})
                self.assertEqual(namespace["summary"]["n_candidates"], 2)
                self.assertEqual(namespace["summary"]["complete"], mode != "legacy_incomplete")
                self.assertEqual(namespace["summary"]["n_ok"], 3)
                checkpoint_paths = set().union(*(set(batch) for batch in commits))
                expected_paths = chip_files(2) | {
                    PREFIX + "incident_2/result.json", PREFIX + "incident_3/result.json",
                }
                summaries = {PREFIX + name for name in (
                    "study.json", "run.json", "summary.json", "candidate_status.csv",
                    "candidate_metadata.csv", "diagnostics.json",
                )}
                self.assertEqual(checkpoint_paths,
                                 summaries if mode == "all_done" else expected_paths | summaries)
                api.create_commit.assert_called_once()
                api.upload_folder.assert_not_called()
                self.assertEqual(namespace["metadata_rows"],
                                 [{"incident_id": 1, "candidate_id": 1},
                                  {"incident_id": 2, "candidate_id": 1}])
                for batch in commits:
                    if PREFIX + "incident_2/result.json" in batch:
                        self.assertTrue(chip_files(2).issubset(batch))


if __name__ == "__main__":
    unittest.main()
