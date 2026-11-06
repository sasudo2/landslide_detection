import contextlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pandas as pd


NOTEBOOK = (
    Path(__file__).resolve().parents[1]
    / "notebooks/pipeline/stage_3_annotation_and_png_export/landslide_download.ipynb"
)


class LandslideDownloadTests(unittest.TestCase):
    def test_missing_sources_are_skipped_without_blocking_available_images(self):
        notebook = json.loads(NOTEBOOK.read_text())
        source = "".join(next(cell for cell in notebook["cells"] if cell["id"] == "3fe29f5a")["source"])
        for all_missing in (False, True):
            with self.subTest(all_missing=all_missing), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                csv_path = root / "landslide.csv"
                pd.DataFrame([
                    dict(incident=incident, candidate=1, label="landslide",
                         min_lon=85, max_lon=86, min_lat=27, max_lat=28)
                    for incident in range(1, 5)
                ]).to_csv(csv_path, index=False)
                files = [] if all_missing else [
                    "raw_images/raw_incidents/incident_1/incident_1_after.tif",
                    "raw_images/raw_incidents/incident_1/incident_1_gee_after.tif",
                    "raw_images/raw_incidents/incident_2/incident_2_gee_after.tif",
                    "landslide/incident_3_candidate_1_after.tif",
                ]
                api = Mock()
                api.list_repo_files.return_value = files
                namespace = dict(
                    LANDSLIDE_CSV=csv_path, pd=pd, np=np, re=re, api=api,
                    HF_REPO="test", HF_REPO_TYPE="dataset", HF_REVISION="main",
                    HF_TIF_ROOT="landslide", HF_RAW_ROOT="raw_images/raw_incidents",
                    TIFF_OUTPUT_DIR=root / "tifs", PNG_OUTPUT_DIR=root / "pngs",
                )
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    exec(compile(source, str(NOTEBOOK), "exec"), namespace)
                    namespace["processing_errors"] = []
                    exec("".join(notebook["cells"][-1]["source"]), namespace)
                self.assertIn("SKIP incident 4 candidate 1", output.getvalue())
                if all_missing:
                    self.assertEqual(namespace["work"], [])
                    self.assertEqual(len(namespace["missing_sources"]), 4)
                    self.assertEqual(namespace["skipped_existing"], 0)
                else:
                    self.assertEqual(
                        [(row["incident"], row["image_source"]) for row in namespace["work"]],
                        [("1", "planet"), ("2", "gee")],
                    )
                    self.assertEqual(namespace["missing_sources"], ["4"])
                    self.assertEqual(namespace["skipped_existing"], 1)
