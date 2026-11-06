import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "ablation"
    / "download_ablation_candidates.sh"
)
SOURCE = SCRIPT.read_text().split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]


class AblationDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.snapshot = self.root / "snapshot"
        self.outdir = self.root / "out"
        self.prefix = "ablations/study_01/optical_only/"
        self.files = {
            self.prefix + "study.json": "study",
            self.prefix + "candidate_status.csv": "status",
            self.prefix + "incident_2/result.json": "checkpoint",
            self.prefix + "incident_2/candidate_1/incident_2_candidate_1_after.tif": "two",
            self.prefix + "incident_10/candidate_1/incident_10_candidate_1_after.tif": "ten",
        }
        for path, content in self.files.items():
            target = self.snapshot / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        # A shared HF snapshot cache can contain files outside this request.
        for path in (
            self.prefix + "incident_999/candidate_1/stale.tif",
            "ablations/study_01/baseline/incident_2/candidate_1/other.tif",
            "candidates/incident_2/candidate_1/production.tif",
        ):
            target = self.snapshot / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("unselected")
        self.api = Mock()
        self.api.dataset_info.return_value = SimpleNamespace(sha="a" * 40)
        self.api.list_repo_tree.side_effect = lambda **kwargs: [
            SimpleNamespace(path=path, blob_id="blob") for path in self.files
        ]
        self.download = Mock(side_effect=lambda **kwargs: str(self.snapshot / kwargs["filename"]))
        self.hub = ModuleType("huggingface_hub")
        self.hub.HfApi = Mock(return_value=self.api)
        self.hub.hf_hub_download = self.download
        self.hub.snapshot_download = Mock(side_effect=AssertionError("Must not scan the full repo"))
        self.errors = ModuleType("huggingface_hub.errors")
        self.errors.HfHubHTTPError = type("HfHubHTTPError", (Exception,), {})
        self.errors.LocalEntryNotFoundError = type("LocalEntryNotFoundError", (Exception,), {})

    def run_script(self, *args):
        output = io.StringIO()
        with (
            patch.dict(sys.modules, {
                "huggingface_hub": self.hub, "huggingface_hub.errors": self.errors,
            }),
            patch.object(sys, "argv", [str(SCRIPT), *args]),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(output),
        ):
            namespace = {}
            exec(compile(SOURCE, str(SCRIPT), "exec"), namespace)
        return namespace, output.getvalue()

    def test_selects_one_variant_and_numeric_incident_range_only(self):
        self.run_script("optical_only", "1", "--outdir", str(self.outdir))
        self.assertEqual((self.outdir / "incident_2/result.json").read_text(), "checkpoint")
        self.assertTrue((self.outdir / "candidate_status.csv").is_file())
        self.assertFalse((self.outdir / "incident_10").exists())
        self.assertFalse((self.outdir / "incident_999").exists())
        self.assertFalse((self.outdir / "baseline").exists())
        self.api.list_repo_tree.assert_called_once_with(
            repo_id="sasudo2/landslides", repo_type="dataset", revision="a" * 40,
            path_in_repo=self.prefix.rstrip("/"), recursive=True,
        )
        requests = [call.kwargs for call in self.download.call_args_list]
        self.assertTrue(all(kwargs["revision"] == "a" * 40 for kwargs in requests))
        self.assertEqual({kwargs["filename"] for kwargs in requests}, {
            path for path in self.files if "incident_10" not in path
        })
        self.assertTrue(all("allow_patterns" not in kwargs for kwargs in requests))
        self.hub.snapshot_download.assert_not_called()

    def test_rerun_restores_missing_files_and_refreshes_metadata(self):
        args = ("optical_only", "--outdir", str(self.outdir))
        self.run_script(*args)
        target = self.outdir / "incident_2/candidate_1/incident_2_candidate_1_after.tif"
        target.unlink()
        (self.outdir / "candidate_status.csv").write_text("old")
        _, output = self.run_script(*args)
        self.assertEqual(target.read_text(), "two")
        self.assertEqual((self.outdir / "candidate_status.csv").read_text(), "status")
        self.assertIn("Copied 2 file(s); 3 unchanged", output)

    def test_custom_study_and_isolated_default_destination(self):
        self.prefix = "ablations/another/sar_only/"
        self.files = {self.prefix + "summary.json": "zero candidates"}
        path = self.snapshot / next(iter(self.files))
        path.parent.mkdir(parents=True)
        path.write_text("zero candidates")
        with contextlib.chdir(self.root):
            _, output = self.run_script("sar_only", "--study-root", "ablations/another")
        self.assertIn("0/0 incident(s)", output)
        destination = self.root / "ablation_candidates/ablations/another/sar_only/summary.json"
        self.assertEqual(destination.read_text(), "zero candidates")
        self.assertEqual(
            self.api.list_repo_tree.call_args.kwargs["path_in_repo"],
            "ablations/another/sar_only",
        )

    def test_invalid_variants_ranges_and_roots_fail_before_network(self):
        for args in (
            ("unknown",), ("optical_only", "0"), ("optical_only", "2", "1"),
            ("optical_only", "--study-root", "../raw"),
            ("optical_only", "--study-root", "/raw"),
        ):
            with self.subTest(args=args), self.assertRaises(SystemExit):
                self.run_script(*args)
        self.api.dataset_info.assert_not_called()

    def test_out_of_range_does_not_download(self):
        with self.assertRaises(SystemExit):
            self.run_script("optical_only", "3")
        self.download.assert_not_called()

    def test_transient_file_failure_retries_only_pinned_file(self):
        error = self.errors.HfHubHTTPError("rate limited")
        error.response = SimpleNamespace(status_code=429, headers={"Retry-After": "3"})
        failed_path = self.prefix + "study.json"
        attempts = []

        def download(**kwargs):
            if kwargs["filename"] == failed_path:
                attempts.append(kwargs)
                if len(attempts) == 1:
                    raise error
            return str(self.snapshot / kwargs["filename"])

        self.download.side_effect = download
        with patch("time.sleep") as sleep:
            self.run_script("optical_only", "--outdir", str(self.outdir))
        sleep.assert_called_once_with(3.0)
        self.assertEqual(self.download.call_count, len(self.files) + 1)
        self.assertEqual(attempts[0], attempts[1])
        self.assertEqual(attempts[0]["revision"], "a" * 40)

    def test_missing_cached_file_is_an_explicit_failure(self):
        (self.snapshot / (self.prefix + "study.json")).unlink()
        with self.assertRaisesRegex(FileNotFoundError, "missing requested file"):
            self.run_script("optical_only", "--outdir", str(self.outdir))

    def test_custom_repository_revision_and_clamped_range(self):
        _, output = self.run_script(
            "optical_only", "1", "99", "--repo", "example/outputs",
            "--revision", "release", "--outdir", str(self.outdir),
        )
        self.api.dataset_info.assert_called_once_with("example/outputs", revision="release")
        self.assertIn("clamping end 99 to 2", output)
        self.assertEqual(self.download.call_args.kwargs["repo_id"], "example/outputs")
        self.assertTrue((self.outdir / "incident_10").is_dir())

    def test_invalid_resolved_sha_fails_before_listing(self):
        self.api.dataset_info.return_value.sha = "main"
        with self.assertRaisesRegex(RuntimeError, "invalid immutable commit"):
            self.run_script("optical_only")
        self.api.list_repo_tree.assert_not_called()
        self.download.assert_not_called()

    def test_permanent_hub_error_is_not_retried_or_hidden(self):
        error = self.errors.HfHubHTTPError("forbidden")
        error.response = SimpleNamespace(status_code=403, headers={})
        self.download.side_effect = error
        with patch("time.sleep") as sleep, self.assertRaisesRegex(
            self.errors.HfHubHTTPError, "forbidden"
        ):
            self.run_script("optical_only", "--outdir", str(self.outdir))
        sleep.assert_not_called()
        self.assertGreaterEqual(self.download.call_count, 1)

    def test_downloads_literal_filenames_without_glob_filtering(self):
        remote = self.prefix + "incident_2/candidate_1/quality[1].json"
        self.files[remote] = "quality metadata"
        (self.snapshot / remote).write_text(self.files[remote])
        _, output = self.run_script("optical_only", "--outdir", str(self.outdir))
        self.assertEqual(
            (self.outdir / "incident_2/candidate_1/quality[1].json").read_text(),
            "quality metadata",
        )
        self.assertEqual(self.download.call_count, len(self.files))
        self.assertIn(f"Processed {len(self.files)}/{len(self.files)} file(s)", output)
        self.hub.snapshot_download.assert_not_called()


if __name__ == "__main__":
    unittest.main()
