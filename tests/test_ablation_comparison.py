import ast
import contextlib
import copy
import csv
import io
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from test_ablation_resume import sources


NOTEBOOK = (
    Path(__file__).resolve().parents[1]
    / "notebooks"
    / "experiments"
    / "ablation"
    / "ablation_compare.ipynb"
)


class AblationComparisonTests(unittest.TestCase):
    def execute_comparison(
        self, *, status="incomplete", strict=False, conflict=False, bad_count=False,
        identical_blobs=False, missing_blob=False, repo_conflict=False,
    ):
        tree = ast.parse(sources(NOTEBOOK)[3])
        constants = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                try:
                    constants[node.targets[0].id] = ast.literal_eval(node.value)
                except ValueError:
                    pass
        self.assertTrue(constants["ALLOW_PARTIAL"])
        variants = constants["VARIANTS"]
        shared = {key: {} for key in constants["SHARED_IDENTITY_KEYS"]}
        shared.update(
            study_schema_version=constants["STUDY_SCHEMA_VERSION"],
            output_schema_version=constants["OUTPUT_SCHEMA_VERSION"],
            detection_algorithm_version=constants["DETECTION_ALGORITHM_VERSION"],
            raw_commit_sha="a" * 40,
            raw_repo="example/raw",
            cohort={"declaration": "df.iloc[900:1000]", "start": 900, "stop": 1000, "count": 100},
            incident_ids=list(range(100)),
            before_file_by_incident={str(i): "planet_before.tif" for i in range(100)},
        )
        files, documents, statuses = set(), {}, {}
        for name, switches in variants.items():
            prefix = f"{constants['STUDY_ROOT']}/{name}/"
            files.update(prefix + filename for filename in (
                "study.json", "run.json", "summary.json", "candidate_status.csv",
                "candidate_metadata.csv", "diagnostics.json",
            ))
            study = copy.deepcopy(shared)
            study.update(variant=name, switches=switches)
            if conflict and name == "no_slic":
                study["raw_commit_sha"] = "b" * 40
            if repo_conflict and name == "no_slic":
                study["raw_repo"] = "example/other"
            documents[prefix + "study.json"] = study
            documents[prefix + "run.json"] = {"status": status}
            failed = 0 if status == "complete" else 1
            documents[prefix + "summary.json"] = {
                "variant": name, "n_incidents": 100, "n_ok": 100 - failed,
                "n_failed": failed, "n_candidates": 0, "n_incidents_with_candidates": 0,
                "complete": not failed, "metadata_complete": True,
            }
            statuses[prefix + "candidate_status.csv"] = [
                {"incident_id": str(i), "status": "failed" if failed and i == 99 else "ok",
                 "n_candidates": "bad" if bad_count and i == 0 else "0"}
                for i in range(100)
            ]
        skip_functions = {"resolve_hf_token", "repo_files", "read_json", "read_csv", "retry_hub"}
        body = []
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module == "huggingface_hub":
                continue
            if isinstance(node, ast.FunctionDef) and node.name in skip_functions:
                continue
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                if node.targets[0].id in {"HF_TOKEN", "API"}:
                    continue
                if node.targets[0].id == "ALLOW_PARTIAL" and strict:
                    node.value = ast.copy_location(ast.Constant(False), node.value)
            body.append(node)
        api = Mock()

        def paths_info(*, repo_id, repo_type, revision, paths):
            entries = [
                SimpleNamespace(
                    path=path,
                    blob_id="same" if identical_blobs or revision == "a" * 40 else "changed",
                )
                for path in paths
            ]
            return entries[:-1] if missing_blob else entries

        api.get_paths_info.side_effect = paths_info
        namespace = {
            "API": api, "repo_files": lambda: files,
            "read_json": lambda path: documents[path], "read_csv": lambda path: statuses[path],
            "retry_hub": lambda action, description: action(),
        }
        output = io.StringIO()
        error = None
        with contextlib.redirect_stdout(output):
            try:
                exec(compile(ast.Module(body=body, type_ignores=[]), str(NOTEBOOK), "exec"), namespace)
            except RuntimeError as exc:
                error = exc
        return api, namespace, output.getvalue(), error

    def test_six_incomplete_runs_publish_flagged_diagnostic_report(self):
        api, _, output, error = self.execute_comparison()
        self.assertIsNone(error)
        api.upload_file.assert_called_once()
        data = api.upload_file.call_args.kwargs["path_or_fileobj"].getvalue().decode()
        rows = list(csv.DictReader(io.StringIO(data)))
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(row["run_complete"] == "no" for row in rows))
        self.assertTrue(all(row["run_status"] == "incomplete" for row in rows))
        self.assertTrue(all(row["n_failed"] == "1" for row in rows))
        self.assertIn("PARTIAL DIAGNOSTIC REPORT", output)
        api.get_paths_info.assert_not_called()

    def test_complete_runs_do_not_emit_partial_warning(self):
        api, _, output, error = self.execute_comparison(status="complete")
        self.assertIsNone(error)
        api.upload_file.assert_called_once()
        self.assertNotIn("WARNING", output)

    def test_strict_mode_still_blocks_incomplete_runs(self):
        api, _, _, error = self.execute_comparison(strict=True)
        self.assertIn("ALLOW_PARTIAL = True", str(error))
        api.upload_file.assert_not_called()

    def test_partial_mode_does_not_bypass_identity_or_accounting_validation(self):
        for options in ({"conflict": True}, {"bad_count": True}):
            with self.subTest(options=options):
                api, _, _, error = self.execute_comparison(**options)
                self.assertIsNotNone(error)
                api.upload_file.assert_not_called()

    def test_different_commits_with_identical_inputs_are_accepted(self):
        api, namespace, output, error = self.execute_comparison(conflict=True, identical_blobs=True)
        self.assertIsNone(error)
        api.upload_file.assert_called_once()
        self.assertEqual(api.get_paths_info.call_count, 10)
        self.assertTrue(all(
            len(call.kwargs["paths"]) == 100
            for call in api.get_paths_info.call_args_list
        ))
        rows = namespace["rows"]
        changed = next(row for row in rows if row["variant"] == "no_slic")
        self.assertEqual(changed["raw_commit_sha"], "b" * 40)
        self.assertEqual(changed["raw_identity_check"], "identical_input_blobs")
        self.assertIn("all 500 selected input files", output)

    def test_changed_or_missing_inputs_block_publication(self):
        for options, message in (
            ({}, "raw input check failed"),
            ({"identical_blobs": True, "missing_blob": True}, "missing file/blob metadata"),
        ):
            with self.subTest(options=options):
                api, _, _, error = self.execute_comparison(conflict=True, **options)
                self.assertIn(message, str(error))
                api.upload_file.assert_not_called()

    def test_different_raw_repositories_are_not_equivalent(self):
        api, _, _, error = self.execute_comparison(repo_conflict=True)
        self.assertIn("raw_repo differs", str(error))
        api.get_paths_info.assert_not_called()
        api.upload_file.assert_not_called()

    def test_strict_complete_comparison_also_accepts_verified_identical_inputs(self):
        api, _, _, error = self.execute_comparison(
            status="complete", strict=True, conflict=True, identical_blobs=True,
        )
        self.assertIsNone(error)
        api.upload_file.assert_called_once()


if __name__ == "__main__":
    unittest.main()
