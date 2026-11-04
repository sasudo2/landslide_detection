import ast
import contextlib
import io
import json
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]
RUN_NOTEBOOKS = sorted(
    path for path in ROOT.glob("ablation_*.ipynb")
    if path.name != "ablation_compare.ipynb"
)
SHA = "abcdef0123456789" * 2 + "abcdef01"


def notebook_code(path):
    notebook = json.loads(path.read_text(encoding="utf-8"))
    sources = []
    for cell in notebook["cells"]:
        if cell["cell_type"] != "code":
            continue
        source = cell.get("source", "")
        source = "".join(source) if isinstance(source, list) else source
        if not source.startswith("%"):
            sources.append(source)
    return ast.parse("\n\n".join(sources), filename=str(path))


def load_resolver(path, api):
    tree = notebook_code(path)
    resolver = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "resolve_immutable_sha"
    )
    namespace = {"re": re, "API": api, "retry_hub": lambda action, description: action()}
    exec(
        compile(ast.Module(body=[resolver], type_ignores=[]), str(path), "exec"),
        namespace,
    )
    return namespace["resolve_immutable_sha"]


class AblationRevisionTests(unittest.TestCase):
    def test_default_revision_resolves_in_every_run(self):
        self.assertEqual(len(RUN_NOTEBOOKS), 6)
        for path in RUN_NOTEBOOKS:
            with self.subTest(notebook=path.name):
                tree = notebook_code(path)
                assignment = next(
                    node for node in tree.body
                    if isinstance(node, ast.Assign)
                    and any(
                        isinstance(target, ast.Name) and target.id == "RAW_COMMIT_SHA"
                        for target in node.targets
                    )
                )
                revision = ast.literal_eval(assignment.value)
                self.assertEqual(revision, "main")
                api = Mock()
                api.repo_info.return_value = SimpleNamespace(sha=SHA)
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    resolved = load_resolver(path, api)("owner/raw", revision)
                self.assertEqual(resolved, SHA)
                api.repo_info.assert_called_once_with(
                    repo_id="owner/raw", repo_type="dataset", revision="main"
                )
                self.assertIn(SHA, output.getvalue())

    def test_branch_case_tag_and_explicit_sha(self):
        for path in RUN_NOTEBOOKS:
            for configured, requested in (
                (" main ", "main"),
                ("Release/Study", "Release/Study"),
                ("v1.0", "v1.0"),
                (SHA, SHA),
                (SHA.upper(), SHA),
            ):
                with self.subTest(notebook=path.name, revision=configured):
                    api = Mock()
                    api.repo_info.return_value = SimpleNamespace(sha=SHA)
                    with contextlib.redirect_stdout(io.StringIO()):
                        resolved = load_resolver(path, api)("owner/raw", configured)
                    self.assertEqual(resolved, SHA)
                    self.assertEqual(api.repo_info.call_args.kwargs["revision"], requested)

    def test_invalid_configuration_fails_before_network(self):
        for path in RUN_NOTEBOOKS:
            for configured in ("", " ", None, "REPLACE_WITH_40_CHARACTER_RAW_COMMIT_SHA"):
                with self.subTest(notebook=path.name, revision=configured):
                    api = Mock()
                    with self.assertRaisesRegex(ValueError, "RAW_COMMIT_SHA"):
                        load_resolver(path, api)("owner/raw", configured)
                    api.repo_info.assert_not_called()

    def test_invalid_or_mismatched_response_is_rejected(self):
        for path in RUN_NOTEBOOKS:
            for returned in (None, "", "main", "123", "0" * 40):
                with self.subTest(notebook=path.name, returned=returned):
                    api = Mock()
                    api.repo_info.return_value = SimpleNamespace(sha=returned)
                    with self.assertRaises(ValueError):
                        load_resolver(path, api)("owner/raw", SHA)

    def test_hub_errors_are_not_hidden(self):
        for path in RUN_NOTEBOOKS:
            with self.subTest(notebook=path.name):
                api = Mock()
                api.repo_info.side_effect = RuntimeError("revision not found")
                with self.assertRaisesRegex(RuntimeError, "revision not found"):
                    load_resolver(path, api)("owner/raw", "missing-branch")

    def test_raw_file_calls_remain_pinned(self):
        for path in RUN_NOTEBOOKS:
            with self.subTest(notebook=path.name):
                tree = notebook_code(path)
                downloads = [
                    node for node in ast.walk(tree)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "hf_hub_download"
                ]
                self.assertTrue(downloads)
                for call in downloads:
                    revision = next(
                        keyword.value for keyword in call.keywords
                        if keyword.arg == "revision"
                    )
                    self.assertEqual(ast.unparse(revision), "study['raw_commit_sha']")
                listing = next(
                    node for node in ast.walk(tree)
                    if isinstance(node, ast.Assign)
                    and any(
                        isinstance(target, ast.Name) and target.id == "raw_files"
                        for target in node.targets
                    )
                )
                self.assertEqual(ast.unparse(listing.value.args[1]), "raw_sha")

    def test_comparison_still_checks_resolved_commit(self):
        tree = notebook_code(ROOT / "ablation_compare.ipynb")
        identity = next(
            node for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "SHARED_IDENTITY_KEYS"
                for target in node.targets
            )
        )
        self.assertIn("raw_commit_sha", ast.literal_eval(identity.value))


if __name__ == "__main__":
    unittest.main()
