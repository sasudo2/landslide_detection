import contextlib
import io
import json
import math
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pandas as pd
import rasterio
from rasterio.transform import from_origin
from rasterio.warp import transform_bounds
from rasterio.windows import Window, from_bounds


NOTEBOOK = (
    Path(__file__).resolve().parents[1]
    / "notebooks/pipeline/stage_3_annotation_and_png_export/landslide_download.ipynb"
)


class LandslideDownloadTests(unittest.TestCase):
    def test_png_respects_rgb_and_bgr_color_metadata(self):
        notebook = json.loads(NOTEBOOK.read_text())
        source = "".join(next(cell for cell in notebook["cells"] if cell["id"] == "3277fb0e")["source"])
        namespace = dict(
            Path=Path, rasterio=rasterio, np=np,
            STRETCH_LO_PCT=2, STRETCH_HI_PCT=98, PNG_BRIGHTNESS_GAMMA=1,
            math=math, transform_bounds=transform_bounds, from_bounds=from_bounds, Window=Window,
        )
        exec(compile(source, str(NOTEBOOK), "exec"), namespace)
        crop_source = "".join(next(cell for cell in notebook["cells"] if cell["id"] == "aceaecc2")["source"])
        exec(compile(crop_source, str(NOTEBOOK), "exec"), namespace)
        for order in ("rgb", "bgr", "undefined", "rgba", "undefined_bgrn"):
            with self.subTest(order=order), tempfile.TemporaryDirectory() as directory:
                tif = Path(directory) / "source.tif"
                crop = Path(directory) / "crop.tif"
                png = Path(directory) / "output.png"
                values = [30, 20, 10] if order in ("bgr", "undefined_bgrn") else [10, 20, 30]
                if order in ("rgba", "undefined_bgrn"):
                    values.append(255)
                with rasterio.open(
                    tif, "w", driver="GTiff", width=2, height=2, count=len(values),
                    dtype="uint8", crs="EPSG:4326", transform=from_origin(0, 2, 1, 1),
                ) as dst:
                    dst.write(np.array([np.full((2, 2), value) for value in values], dtype=np.uint8))
                    color = rasterio.enums.ColorInterp
                    dst.colorinterp = (
                        (color.blue, color.green, color.red) if order == "bgr"
                        else (color.red, color.green, color.blue) if order == "rgb"
                        else (color.red, color.green, color.blue, color.alpha) if order == "rgba"
                        else (color.undefined,) * 4 if order == "undefined_bgrn"
                        else (color.undefined,) * 3
                    )
                row = dict(incident="1", candidate="1", min_lon=0, max_lon=2, min_lat=0, max_lat=2)
                namespace["crop_raw_after_to_tif"](tif, crop, row)
                with rasterio.open(tif) as src, rasterio.open(crop) as cropped:
                    self.assertEqual(cropped.colorinterp, src.colorinterp)
                namespace["write_rgb_png"](crop, png)
                with rasterio.open(png) as src:
                    self.assertEqual(src.read()[:, 0, 0].tolist(), [10, 20, 30])

    def test_png_handles_default_gray_metadata_for_analytic_tiff(self):
        notebook = json.loads(NOTEBOOK.read_text())
        source = "".join(next(cell for cell in notebook["cells"] if cell["id"] == "3277fb0e")["source"])
        namespace = dict(
            Path=Path, rasterio=rasterio, np=np,
            STRETCH_LO_PCT=2, STRETCH_HI_PCT=98, PNG_BRIGHTNESS_GAMMA=1,
            math=math, transform_bounds=transform_bounds, from_bounds=from_bounds, Window=Window,
        )
        exec(compile(source, str(NOTEBOOK), "exec"), namespace)
        crop_source = "".join(next(cell for cell in notebook["cells"] if cell["id"] == "aceaecc2")["source"])
        exec(compile(crop_source, str(NOTEBOOK), "exec"), namespace)
        with tempfile.TemporaryDirectory() as directory:
            tif = Path(directory) / "analytic.tif"
            crop = Path(directory) / "crop.tif"
            png = Path(directory) / "output.png"
            pixels = np.array([
                [[500, 500], [500, 0]],  # blue
                [[1000, 0], [0, 0]],  # green
                [[0, 1000], [1000, 1000]],  # red
                [[2000, 2000], [2000, 2000]],  # near infrared
            ], dtype=np.uint16)
            with rasterio.open(
                tif, "w", driver="GTiff", width=2, height=2, count=4,
                dtype="uint16", crs="EPSG:4326", transform=from_origin(0, 2, 1, 1),
            ) as dst:
                dst.write(pixels)
            row = dict(incident="1", candidate="1", min_lon=0, max_lon=2, min_lat=0, max_lat=2)
            namespace["crop_raw_after_to_tif"](tif, crop, row)
            namespace["write_rgb_png"](crop, png)
            with rasterio.open(png) as src:
                np.testing.assert_allclose(src.read()[:, 0, 0], [0, 255, 255], atol=1)

    def test_crop_shifts_inside_edges_without_changing_dimensions(self):
        notebook = json.loads(NOTEBOOK.read_text())
        source = "".join(next(cell for cell in notebook["cells"] if cell["id"] == "aceaecc2")["source"])
        namespace = dict(
            math=math, Path=Path, rasterio=rasterio,
            transform_bounds=transform_bounds, from_bounds=from_bounds, Window=Window,
        )
        exec(compile(source, str(NOTEBOOK), "exec"), namespace)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw.tif"
            transform = from_origin(0, 10, 1, 1)
            pixels = np.arange(100, dtype=np.uint16).reshape(1, 10, 10)
            with rasterio.open(
                raw, "w", driver="GTiff", width=10, height=10, count=1,
                dtype="uint16", crs="EPSG:4326", transform=transform,
            ) as dst:
                dst.write(pixels)
            cases = [
                (-2, 2, 0, 2), (8, 2, 6, 2),
                (2, -2, 2, 0), (2, 8, 2, 6),
                (-2, -2, 0, 0), (8, 8, 6, 6), (2, 2, 2, 2),
            ]
            for col, row, expected_col, expected_row in cases:
                with self.subTest(col=col, row=row):
                    annotation = dict(
                        incident="1", candidate="1", min_lon=col, max_lon=col + 4,
                        max_lat=10 - row, min_lat=10 - row - 4,
                    )
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        cropped = namespace["crop_raw_after_to_tif"](raw, root / "crop.tif", annotation)
                    with rasterio.open(cropped) as src:
                        self.assertEqual((src.width, src.height), (4, 4))
                        self.assertEqual(src.transform, from_origin(expected_col, 10 - expected_row, 1, 1))
                        np.testing.assert_array_equal(
                            src.read(), pixels[:, expected_row:expected_row + 4, expected_col:expected_col + 4],
                        )
                    self.assertEqual("SHIFT" in output.getvalue(), (col, row) != (expected_col, expected_row))
            oversized = dict(incident="1", candidate="1", min_lon=0, max_lon=11, min_lat=0, max_lat=10)
            with self.assertRaisesRegex(ValueError, "larger than its raw after-image"):
                namespace["crop_raw_after_to_tif"](raw, root / "oversized.tif", oversized)

    def test_missing_sources_are_skipped_without_blocking_available_images(self):
        notebook = json.loads(NOTEBOOK.read_text())
        source = "".join(next(cell for cell in notebook["cells"] if cell["id"] == "b2083ea1")["source"])
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
