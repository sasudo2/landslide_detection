import ast
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from test_ablation_resume import helpers, sources
from test_ablation_revisions import RUN_NOTEBOOKS


class AblationStorageTests(unittest.TestCase):
    def test_raw_downloads_use_only_private_incident_cache(self):
        for notebook in RUN_NOTEBOOKS:
            with self.subTest(notebook=notebook.name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                namespace = helpers(notebook)
                download = Mock(return_value=str(root / "file.tif"))
                namespace.update({
                    "RAW_ROOT": "raw", "RAW_REPO": "owner/raw", "HF_TOKEN": "test-token",
                    "study": {"raw_commit_sha": "a" * 40}, "LOCAL_RAW_ROOT": root,
                    "hf_hub_download": download,
                    "retry_hub": lambda action, description: action(),
                })
                paths = namespace["fetch_paths"](123, {123: "planet_before.tif"})
                self.assertEqual(len(paths), 5)
                for call in download.call_args_list:
                    self.assertEqual(call.kwargs["cache_dir"], str(root / "incident_123"))
                    self.assertEqual(call.kwargs["revision"], "a" * 40)

    def test_raw_cleanup_after_export_and_after_partial_download_or_processing_failure(self):
        for notebook in RUN_NOTEBOOKS:
            for mode in ("success", "download_failure", "detection_failure", "export_failure"):
                with self.subTest(notebook=notebook.name, mode=mode), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    raw = root / "raw" / "incident_123"
                    candidates = root / "candidates"
                    candidates.mkdir()
                    shared = root / "shared"
                    shared.mkdir()
                    (shared / "unrelated.tif").write_bytes(b"keep")
                    namespace = {
                        "LOCAL_RAW_ROOT": root / "raw", "LOCAL_VARIANT": candidates,
                        "before_by_incident": {123: "planet_before.tif"},
                        "shutil": shutil,
                        "np": SimpleNamespace(count_nonzero=lambda mask: 1),
                    }

                    def fetch(incident_id, before):
                        raw.mkdir(parents=True)
                        (raw / "partial.tif").write_bytes(b"raw")
                        if mode == "download_failure":
                            raise OSError("download interrupted")
                        return {"before": raw / "partial.tif"}

                    def detect(paths):
                        self.assertTrue(paths["before"].is_file())
                        if mode == "detection_failure":
                            raise ValueError("coverage error")
                        return {"n_tests": 1, "z_crit": 2, "mask": [True]}

                    def export(incident_id, paths, result, folder):
                        self.assertTrue(paths["before"].is_file())
                        if mode == "export_failure":
                            raise OSError("chip write failed")
                        (folder / "candidate.tif").write_bytes(b"candidate")
                        return [{"incident_id": 123, "candidate_id": 1}]

                    namespace.update(fetch_paths=fetch, detect=detect, export_candidates=export)
                    tree = ast.parse(sources(notebook)[8])
                    function = next(n for n in tree.body
                                    if isinstance(n, ast.FunctionDef) and n.name == "process_incident")
                    exec(compile(ast.Module(body=[function], type_ignores=[]),
                                 str(notebook), "exec"), namespace)
                    if mode == "success":
                        namespace["process_incident"](123)
                        self.assertTrue((candidates / "incident_123" / "candidate.tif").is_file())
                    else:
                        with self.assertRaises((ValueError, OSError)):
                            namespace["process_incident"](123)
                    self.assertFalse(raw.exists())
                    self.assertTrue((shared / "unrelated.tif").is_file())

    def test_notebooks_have_only_one_remote_commit_and_no_batch_setting(self):
        for notebook in RUN_NOTEBOOKS:
            with self.subTest(notebook=notebook.name):
                source = "\n".join(s for s in sources(notebook) if not s.startswith("%"))
                tree = ast.parse("\n".join(
                    s for c, s in zip(json.loads(notebook.read_text())["cells"], sources(notebook))
                    if c["cell_type"] == "code" and not s.startswith("%")
                ))
                writes = [
                    n for n in ast.walk(tree) if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr in {"create_commit", "upload_folder", "upload_file"}
                ]
                self.assertEqual(len(writes), 1)
                self.assertEqual(writes[0].func.attr, "create_commit")
                self.assertNotIn("UPLOAD_BATCH_FILES", source)


if __name__ == "__main__":
    unittest.main()
