import ast
import contextlib
import csv
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin


PIPELINE = Path(__file__).resolve().parents[1] / "notebooks" / "pipeline"
STAGE_2 = PIPELINE / "stage_2_candidate_collection"
SCRIPT = STAGE_2 / "deduplicate_candidates.py"
SPEC = importlib.util.spec_from_file_location("candidate_dedup", SCRIPT)
dedup = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dedup
SPEC.loader.exec_module(dedup)


class CandidateDedupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def candidate(self, incident, candidate, x=85, crs="EPSG:4326", y=28, pixel=0.0001):
        folder = self.root / f"incident_{incident}" / f"candidate_{candidate}"
        folder.mkdir(parents=True)
        with rasterio.open(
            folder / f"incident_{incident}_candidate_{candidate}_after.tif", "w",
            driver="GTiff", width=10, height=10, count=1, dtype="uint8",
            crs=crs, transform=from_origin(x, y, pixel, pixel),
        ) as dst:
            dst.write(np.ones((1, 10, 10), dtype=np.uint8))
        (folder / "before.tif").write_bytes(b"untouched")
        return folder

    def run_script(self, *args):
        with contextlib.redirect_stdout(io.StringIO()):
            dedup.main([str(self.root), *args])

    def test_preview_keeps_all_and_numeric_order_selects_representative(self):
        later = self.candidate(10, 1)
        first = self.candidate(2, 2)
        self.candidate(2, 10)
        self.run_script()
        self.assertTrue(later.is_dir())
        self.assertTrue(first.is_dir())
        with (self.root / "candidate_dedup_report.csv").open() as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["kept_folder"] == "incident_2/candidate_2" for row in rows))

    def test_delete_removes_only_duplicate_candidates_and_preserves_metadata(self):
        first = self.candidate(2, 1)
        duplicate = self.candidate(10, 1)
        distant = self.candidate(20, 1, x=86)
        metadata = self.root / "summary.json"
        metadata.write_text("original")
        checkpoint = duplicate.parent / "result.json"
        checkpoint.write_text("original checkpoint")
        self.run_script("--delete")
        self.assertFalse(duplicate.exists())
        self.assertTrue(first.is_dir())
        self.assertTrue(distant.is_dir())
        self.assertEqual(metadata.read_text(), "original")
        self.assertTrue(checkpoint.is_file())
        self.run_script("--delete")
        self.assertTrue(first.is_dir())

    def test_bad_raster_aborts_before_any_deletion(self):
        first = self.candidate(1, 1)
        duplicate = self.candidate(2, 1)
        broken = self.root / "incident_3/candidate_1"
        broken.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            self.run_script("--delete")
        self.assertTrue(first.is_dir())
        self.assertTrue(duplicate.is_dir())

    def test_projected_raster_bounds_are_transformed(self):
        self.candidate(1, 1, x=0, y=100, pixel=10, crs="EPSG:3857")
        candidates = dedup.scan_candidates(self.root)
        left, bottom, right, top = candidates[0].bounds
        self.assertAlmostEqual((left + right) / 2, 0.0004491576, places=7)
        self.assertAlmostEqual((bottom + top) / 2, 0.0004491576, places=7)
        self.assertAlmostEqual(left, 0)
        self.assertAlmostEqual(bottom, 0)
        self.assertAlmostEqual(right, 0.0008983153, places=7)
        self.assertAlmostEqual(top, 0.0008983153, places=7)

    def test_iou_threshold_is_inclusive_and_compares_only_kept_candidates(self):
        candidates = [
            dedup.Candidate(Path("first"), (0, 0, 3, 1)),
            dedup.Candidate(Path("second"), (1, 0, 4, 1)),
            dedup.Candidate(Path("third"), (2, 0, 5, 1)),
        ]
        kept, duplicates = dedup.find_duplicates(candidates, 0.5)
        self.assertEqual([item.folder.name for item in kept], ["first", "third"])
        self.assertEqual(len(duplicates), 1)
        self.assertEqual(duplicates[0][2], 0.5)

    def test_invalid_options_and_report_inside_candidate_are_rejected(self):
        folder = self.candidate(1, 1)
        for args in (["--iou", "0"], ["--iou", "nan"],
                     ["--report", str(folder / "report.csv")]):
            with self.subTest(args=args), self.assertRaises(SystemExit):
                with contextlib.redirect_stderr(io.StringIO()):
                    self.run_script(*args)
        self.assertTrue(folder.is_dir())

    def test_symlink_candidate_is_rejected(self):
        first = self.candidate(1, 1)
        (first.parent / "candidate_2").symlink_to(first, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.run_script("--delete")
        self.assertTrue(first.is_dir())

    def test_iou_matches_annotation_notebook_without_resizing(self):
        notebook = json.loads(
            (PIPELINE / "stage_3_annotation_and_png_export" / "annotation_dedup_build.ipynb").read_text()
        )
        source = notebook["cells"][2]["source"]
        source = "".join(source) if isinstance(source, list) else source
        functions = [
            node for node in ast.parse(source).body
            if isinstance(node, ast.FunctionDef) and node.name == "iou"
        ]
        namespace = {}
        exec(compile(ast.Module(body=functions, type_ignores=[]), "<notebook>", "exec"), namespace)
        inputs = [(85, 27, 85.002, 27.003), (85.001, 27.001, 85.003, 27.004)]
        expected = []
        for bounds in inputs:
            left, bottom, right, top = bounds
            box = {
                "min_lon": left, "min_lat": bottom, "max_lon": right, "max_lat": top,
            }
            expected.append(box)
            self.assertEqual(
                dedup.validate_bounds(bounds),
                (box["min_lon"], box["min_lat"], box["max_lon"], box["max_lat"]),
            )
        self.assertAlmostEqual(
            dedup.iou(*inputs),
            namespace["iou"](*expected),
        )

    def test_missing_crs_and_invalid_geographic_bounds_are_errors(self):
        self.candidate(1, 1, crs=None)
        with self.assertRaisesRegex(ValueError, "no CRS"):
            self.run_script("--delete")
        for bounds in ((0, 0, float("nan"), 1), (10, 0, 5, 1), (-179, 0, 179, 1)):
            with self.subTest(bounds=bounds), self.assertRaises(ValueError):
                dedup.validate_bounds(bounds)

    def test_nearby_nonoverlapping_actual_boxes_are_not_duplicates(self):
        first = self.candidate(1, 1)
        second = self.candidate(2, 1, x=85.002)
        original_files = {
            path: path.read_bytes()
            for folder in (first, second) for path in folder.iterdir()
        }
        self.run_script("--delete")
        for path, content in original_files.items():
            self.assertEqual(path.read_bytes(), content)
        candidates = dedup.scan_candidates(self.root)
        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0].bounds, (85.0, 27.999, 85.001, 28.0))

    def test_actual_box_overlap_controls_deletion(self):
        first = self.candidate(1, 1)
        overlap = self.candidate(2, 1, x=85.0002)
        separate = self.candidate(3, 1, x=85.002)
        self.run_script("--delete")
        self.assertTrue(first.is_dir())
        self.assertFalse(overlap.exists())
        self.assertTrue(separate.is_dir())


if __name__ == "__main__":
    unittest.main()
