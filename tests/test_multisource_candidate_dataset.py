import contextlib
import io
import json
import math
import os
import re
import shutil
import tempfile
import threading
import time
import unittest
import zipfile
from datetime import date, datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urljoin, urlsplit

import nbformat
import numpy as np
import pandas as pd
import rasterio
import requests
from pyproj import CRS, Transformer
from rasterio.enums import Resampling
from rasterio.transform import from_bounds
from rasterio.vrt import WarpedVRT
from scipy import ndimage
from shapely.geometry import Polygon


ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK = ROOT / "notebooks/pipeline/stage_1_data_acquisition/multisource_candidate_dataset.ipynb"


def load_helpers():
    import hashlib
    import calendar
    import logging
    import time

    class CloudError(Exception):
        pass

    class Progress:
        def __init__(self, iterable=None, **kwargs):
            self.iterable = iterable
        def __iter__(self):
            return iter(self.iterable)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def update(self, count):
            pass

    namespace = dict(
        calendar=calendar, contextlib=contextlib, hashlib=hashlib, json=json,
        math=math, os=os, re=re, shutil=shutil, tempfile=tempfile, time=time,
        zipfile=zipfile, date=date, datetime=datetime, timedelta=timedelta,
        timezone=timezone, Path=Path, urljoin=urljoin, urlsplit=urlsplit,
        np=np, pd=pd, rasterio=rasterio, requests=requests, CRS=CRS,
        Transformer=Transformer, Resampling=Resampling, from_bounds=from_bounds,
        WarpedVRT=WarpedVRT, ndimage=ndimage, Polygon=Polygon,
        LOG=logging.getLogger("test_multisource"), ee=SimpleNamespace(EEException=CloudError),
        HfHubHTTPError=CloudError, tqdm=Progress,
        ThreadPoolExecutor=ThreadPoolExecutor, as_completed=as_completed,
        deepcopy=deepcopy, local=threading.local,
    )
    cells = nbformat.read(NOTEBOOK, as_version=4).cells
    selected = {
        "config", "io", "input_grid", "raster_planet", "gee_layers", "ndvi",
        "state_processing", "archives",
    }
    for cell in cells:
        if cell.cell_type != "code":
            continue
        marker = cell.source.splitlines()[0]
        if marker.removeprefix("# cell: ") in selected:
            exec(compile(cell.source, str(NOTEBOOK), "exec"), namespace)
    return namespace


class MultisourceCandidateDatasetTests(unittest.TestCase):
    def setUp(self):
        self.ns = load_helpers()
        self.config = self.ns["CONFIG"]
        self.config.update(MIN_FREE_GB=0, DOWNLOAD_WORKSPACE_GB=0, RETRY_BASE_SECONDS=0)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in (".scratch", ".staging", ".backup", ".calibration", "candidates", "archives"):
            (self.root / name).mkdir()
        self.ns.update(ROOT=self.root, RUN_SIGNATURE="test-run", HF=Mock())
        self.row = dict(
            incident_id="12", candidate_id="3", key="12_3", label="not_landslide",
            west=85.0, east=85.01, south=27.0, north=27.01,
            original_incident_date="2024-12-17", shifted_incident_date="2018-12-17",
            year_shifted=True, date_used="shifted", effective_incident_date="2018-12-17",
        )
        self.grid = self.ns["grid_for_row"](self.row)
        self.record = {
            **self.row, "grid": self.grid, "status": "pending", "archive": None,
            "layers": {name: "pending" for name in self.ns["LAYERS"]},
            "windows": self.ns["season_windows"]("2018-12-17"),
        }
        self.ns["STATE"] = {
            "signature": "test-run", "records": {"12_3": self.record}, "archives": [],
        }

    def complete_attempt(self, row, grid, folder, incident_date, terrain_cache=None):
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        windows = self.ns["season_windows"](incident_date)
        metadata = {}
        for layer in self.ns["LAYERS"]:
            count = 4 if layer.startswith("planet_") else 2 if layer.startswith("s1_") else 1
            info = (
                {"band_order": ["red", "green", "blue", "alpha"],
                 "used_for_ndvi": False, "product_type": "visual"}
                if count == 4 else {"units": "dB"}
            )
            descriptions = info.get("band_order", ["VV", "VH"] if count == 2 else [layer])
            data = np.ones((count, grid["height"], grid["width"]), dtype=np.float32)
            if count == 4:
                data *= np.array([30, 50, 70, 255], dtype=np.float32)[:, None, None]
            self.ns["write_grid"](
                self.ns["raster_path"](folder, row, layer),
                data,
                grid, self.ns["layer_fingerprint"](row, grid, layer, windows), info, descriptions,
            )
            metadata[layer] = info
        return {
            "windows": windows, "layers": {name: "ok" for name in self.ns["LAYERS"]},
            "layer_metadata": metadata, "errors": {},
        }

    def publish_complete(self):
        self.ns["pull_all_layers"] = self.complete_attempt
        self.ns["check_pair"] = Mock(return_value={"ndvi_check_result": "pass"})
        record = self.ns["process_record"](self.record)
        self.ns["STATE"]["records"][record["key"]] = record
        return record

    def s2_attempt(self, row, grid, folder, side, window, windows):
        metadata = {
            "collection": self.config["S2_COLLECTION"], "source_bands": ["B4", "B8"],
            "band_order": ["red", "nir"], "reflectance_scales": [1, 1],
            "reflectance_offsets": [0, 0], "mask_source": "Sentinel-2 SCL",
            "scene_count": 2, "scene_ids": ["scene1", "scene2"], "window": window,
        }
        shape = (grid["height"], grid["width"])
        values = (.4, .2) if window["start"].startswith("2019") else (.2, .6)
        data = np.array([np.full(shape, value, dtype=np.float32) for value in values])
        layer = f"s2_screen_{side}"
        self.ns["write_grid"](
            self.ns["raster_path"](folder, row, layer), data, grid,
            self.ns["layer_fingerprint"](row, grid, layer, windows), metadata, ["red", "nir"],
        )
        return metadata

    def test_notebook_schema_and_all_code_syntax(self):
        notebook = nbformat.read(NOTEBOOK, as_version=4)
        nbformat.validate(notebook)
        for cell in notebook.cells:
            if cell.cell_type == "code":
                compile(cell.source, str(NOTEBOOK), "exec")
                self.assertIsNone(cell.execution_count)
                self.assertEqual(cell.outputs, [])

    def test_actual_csv_schema_and_unique_pairs(self):
        with contextlib.redirect_stdout(io.StringIO()), self.assertLogs("test_multisource", level="WARNING"):
            frame, rejected = self.ns["read_candidates"](ROOT / "data/annotations/non_landslide.csv")
        self.assertEqual(len(rejected), 162)
        self.assertEqual(len(frame), 5977)
        self.assertTrue(rejected.validation_error.str.contains("invalid incident_date").all())
        self.assertTrue(rejected.incident_date.isna().all())
        self.assertTrue(frame.key.is_unique)
        self.assertEqual(set(frame.label), {"not_landslide"})

    def test_invalid_rows_and_duplicate_pairs_are_reported(self):
        rows = [
            dict(incident=1, candidate=1, incident_date="2024-01-01",
                 min_lon=85, max_lon=86, min_lat=27, max_lat=28),
            dict(incident=2, candidate=1, incident_date="invalid",
                 min_lon=85, max_lon=86, min_lat=27, max_lat=28),
            dict(incident=3, candidate=1, incident_date="2024-01-01",
                 min_lon=86, max_lon=85, min_lat=27, max_lat=28),
        ]
        rows.append(rows[0].copy())
        path = self.root / "input.csv"
        pd.DataFrame(rows).to_csv(path, index=False)
        with contextlib.redirect_stdout(io.StringIO()):
            valid, rejected = self.ns["read_candidates"](path)
        self.assertEqual(len(valid), 0)
        self.assertEqual(len(rejected), 4)
        self.assertIn("duplicate", rejected.iloc[0].validation_error)

    def test_optional_blank_columns_remain_json_serializable(self):
        path = self.root / "optional.csv"
        pd.DataFrame([{
            "incident": 1, "candidate": 2, "incident_date": "2024-01-01",
            "min_lon": 85, "max_lon": 86, "min_lat": 27, "max_lat": 28,
            "label": None, "notes": None,
        }]).to_csv(path, index=False)
        with contextlib.redirect_stdout(io.StringIO()):
            frame, rejected = self.ns["read_candidates"](path)
        self.assertEqual(len(rejected), 0)
        json.dumps(frame.to_dict(orient="records"), allow_nan=False)

    def test_config_requires_calendar_quarters_and_rejects_invalid_ndvi(self):
        self.config["DRY_SEASON_MONTHS"] = [11, 12, 1]
        with self.assertRaisesRegex(ValueError, "calendar-quarter"):
            self.ns["validate_config"]()
        self.config["DRY_SEASON_MONTHS"] = [10, 11, 12]
        self.config["NDVI_DROP_THRESHOLD"] = -.1
        with self.assertRaisesRegex(ValueError, "NDVI drop"):
            self.ns["validate_config"]()

    def test_worker_count_is_a_positive_integer(self):
        for value in (0, -1, 1.5, True, "4"):
            self.config["MAX_WORKERS"] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "MAX_WORKERS"):
                self.ns["validate_config"]()
        self.config["MAX_WORKERS"] = 1
        self.ns["validate_config"]()

    def test_exact_footprint_shape_and_generic_utm(self):
        grid = self.grid
        self.assertEqual((grid["height"], grid["width"]), (333, 333))
        self.assertEqual(grid["epsg"], 32645)
        self.assertAlmostEqual(grid["pixel_size_m"], 1000 / 333, places=8)
        x0, y0, x1, y1 = grid["bounds_utm"]
        self.assertAlmostEqual(x1 - x0, 1000)
        self.assertAlmostEqual(y1 - y0, 1000)
        for lon, lat, epsg in ((81, 28, 32644), (85, 28, 32645), (116, -8, 32750)):
            row = {**self.row, "west": lon - .01, "east": lon + .01,
                   "south": lat - .01, "north": lat + .01}
            other = self.ns["grid_for_row"](row)
            self.assertEqual(other["epsg"], epsg)
            self.assertEqual((other["height"], other["width"]), (333, 333))
            to_utm = Transformer.from_crs(4326, epsg, always_xy=True)
            cx, cy = to_utm.transform(lon, lat)
            bounds = other["bounds_utm"]
            self.assertAlmostEqual((bounds[0] + bounds[2]) / 2, cx)
            self.assertAlmostEqual((bounds[1] + bounds[3]) / 2, cy)

    def test_windows_strictly_exclude_event_and_handle_boundaries(self):
        cases = {
            "2024-07-01": ("2023-10-01", "2024-10-01"),
            "2024-10-01": ("2023-10-01", "2025-10-01"),
            "2024-12-31": ("2023-10-01", "2025-10-01"),
            "2025-01-01": ("2024-10-01", "2025-10-01"),
        }
        for event, (before, after) in cases.items():
            with self.subTest(event=event):
                result = self.ns["season_windows"](event)
                self.assertEqual(result["before"]["start"], before)
                self.assertEqual(result["after"]["start"], after)
                self.assertLess(date.fromisoformat(result["before"]["end_inclusive"]), date.fromisoformat(event))
                self.assertGreater(date.fromisoformat(result["after"]["start"]), date.fromisoformat(event))

    def test_shift_is_deterministic_restricted_and_handles_leap_day(self):
        frame = pd.DataFrame([
            dict(original_incident_date="2024-02-29", label="not_landslide"),
            dict(original_incident_date="2024-03-01", label="landslide"),
        ])
        self.config.update(YEAR_SHIFT_FRACTION=1, YEAR_SHIFT_ONLY_LABEL="not_landslide")
        result = self.ns["shift_candidates"](frame)
        self.assertEqual(result.iloc[0].shifted_incident_date, "2018-02-28")
        self.assertTrue(result.iloc[0].year_shifted)
        self.assertFalse(result.iloc[1].year_shifted)
        pd.testing.assert_frame_equal(result, self.ns["shift_candidates"](frame))
        with contextlib.redirect_stdout(io.StringIO()), self.assertLogs("test_multisource", level="WARNING"):
            full, _ = self.ns["read_candidates"](ROOT / "data/annotations/non_landslide.csv")
        self.config.update(YEAR_SHIFT_FRACTION=.3, YEAR_SHIFT_ONLY_LABEL=None)
        shifted = self.ns["shift_candidates"](full)
        self.assertEqual(int(shifted.year_shifted.sum()), round(len(full) * .3))
        dry = self.ns["select_run_rows"](shifted)
        self.assertEqual(len(dry), 3)
        self.assertTrue(dry.year_shifted.any())

    def test_mosaic_selection_accepts_visual_exact_quarter(self):
        window = self.ns["season_windows"]("2024-07-01")["after"]
        catalog = [
            {"id": "q3", "name": "global_quarterly_2024q3_mosaic", "datatype": "byte"},
            {"id": "q4", "name": "global_quarterly_2024q4_mosaic", "datatype": "byte"},
        ]
        self.assertEqual(self.ns["find_mosaic"](catalog, window)["id"], "q4")
        with self.assertRaises(self.ns["MissingLayer"]):
            self.ns["find_mosaic"](catalog[:1], window)
        with self.assertRaisesRegex(ValueError, "Multiple"):
            self.ns["find_mosaic"](catalog + [catalog[1]], window)

    def test_selects_users_global_quarterly_series_not_analytic_monthly(self):
        series = [
            {"id": "431b62a0-eaf9-45e7-acf1-d58278176d52", "name": "Global Monthly"},
            {"id": "060c47b7-57fb-429e-9e5b-bee1c89c6973", "name": "Global Quarterly"},
            {"id": "f88b3bd6-89d6-44c1-92a9-1b418d5394ea",
             "name": "PS Monthly Sen2_normalized_analytic Partner Plan"},
        ]
        with contextlib.redirect_stdout(io.StringIO()):
            result = self.ns["choose_planet_series"](series)
        self.assertEqual(result["id"], series[1]["id"])
        self.config["PLANET_SERIES_ID"] = series[2]["id"]
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, "must be Global Quarterly"):
                self.ns["choose_planet_series"](series)

    def test_missing_quarter_reports_coverage(self):
        catalogue = [
            {"id": "q4", "name": "global_quarterly_2017q4_mosaic"},
        ]
        with contextlib.redirect_stdout(io.StringIO()), self.assertLogs("test_multisource", level="WARNING"):
            missing = self.ns["report_quarterly_coverage"]([self.record], catalogue)
        self.assertIn("2019-10", missing)
        self.assertNotIn("2017-10", missing)

    def test_planet_band_metadata_respects_permutation_and_never_used_for_ndvi(self):
        path = self.root / "bands.tif"
        with rasterio.open(
            path, "w", driver="GTiff", width=2, height=2, count=4, dtype="uint16",
            crs="EPSG:32645", transform=from_bounds(0, 0, 2, 2, 2, 2),
        ) as dst:
            dst.write(np.ones((4, 2, 2), dtype=np.uint16))
        with rasterio.open(path) as src:
            with self.assertRaisesRegex(ValueError, "Missing RGB"):
                self.ns["planet_band_metadata"](src, {})
            result = self.ns["planet_band_metadata"](
                src, {"bands": ["nir", "red", "green", "blue"]},
            )
        self.assertEqual(result["band_order"], ["nir", "red", "green", "blue"])
        self.assertEqual(result["source_scales"], [1] * 4)
        self.assertFalse(result["used_for_ndvi"])

    def test_ndvi_reads_named_s2_bands_and_scales(self):
        folder = self.root / "spectral"
        folder.mkdir()
        path = folder / "test.tif"
        order = ["nir", "red", "green", "blue", "alpha"]
        metadata = dict(
            band_order=order, reflectance_scales=[.0001] * 5,
            reflectance_offsets=[0] * 5,
        )
        data = np.array([np.full((333, 333), value, dtype=np.float32)
                         for value in (6000, 2000, 1500, 1000, 255)])
        data[0, 0, 0] = np.nan
        self.ns["write_grid"](path, data, self.grid, "fingerprint", metadata, order)
        ndvi, valid, method = self.ns["ndvi_from_raster"](path)
        self.assertAlmostEqual(float(ndvi[1, 1]), .5, places=6)
        self.assertFalse(valid[0, 0])
        self.assertEqual(int(valid.sum()), 333 * 333 - 1)
        self.assertIn("reflectance", method)

    def test_planet_quad_warp_keeps_all_bands_and_no_signed_url_in_metadata(self):
        source = self.root / "source_quad.tif"
        bands = ["blue", "green", "red", "nir", "alpha"]
        values = (1000, 2000, 2000, 6000, 65535)
        with rasterio.open(
            source, "w", driver="GTiff", width=333, height=333, count=5,
            dtype="uint16", crs="EPSG:32645",
            transform=rasterio.Affine(*self.grid["transform"]), nodata=0,
        ) as dst:
            dst.write(np.array([np.full((333, 333), value, dtype=np.uint16) for value in values]))
            dst.descriptions = tuple(bands)
        self.ns.update(
            PLANET=Mock(), PLANET_SERIES={"id": "series1"},
            MOSAIC_CATALOGUE=[{"id": "m1", "name": "global_quarterly_2017q4_mosaic"}],
            http_json=Mock(return_value={}),
            planet_pages=Mock(return_value=[
                {"_links": {"download": "https://storage.example/quad?secret=not-exported"}},
            ]),
            download_file=lambda session, url, output: shutil.copy2(source, output),
        )
        folder = self.root / "planet_result"
        folder.mkdir()
        window = self.record["windows"]["before"]
        metadata = self.ns["pull_planet"](
            self.row, self.grid, folder, "planet_before", window, "fingerprint",
        )
        output = folder / "12_3_planet_before.tif"
        with rasterio.open(output) as src:
            self.assertEqual(src.count, 5)
            self.assertEqual(src.read()[:, 10, 10].tolist(), list(values))
            self.assertEqual(src.descriptions, tuple(bands))
            self.assertNotIn("not-exported", json.dumps(src.tags()))
        self.assertEqual(metadata["mosaic_name"], "global_quarterly_2017q4_mosaic")
        with self.assertRaisesRegex(ValueError, "not the NDVI source"):
            self.ns["ndvi_from_raster"](output)

    def test_global_quarterly_visual_rgba_download_and_alpha_exclusion(self):
        source = self.root / "visual.tif"
        with rasterio.open(
            source, "w", driver="GTiff", width=333, height=333, count=4,
            dtype="uint8", crs="EPSG:32645", transform=rasterio.Affine(*self.grid["transform"]),
        ) as dst:
            data = np.array([np.full((333, 333), value, dtype=np.uint8) for value in (30, 50, 70, 255)])
            data[:, 0, 0] = 0
            dst.write(data)
            dst.colorinterp = (rasterio.enums.ColorInterp.undefined,) * 4
        self.ns.update(
            PLANET=Mock(), PLANET_SERIES={"id": "global-quarterly"},
            MOSAIC_CATALOGUE=[{"id": "q4", "name": "global_quarterly_2017q4_mosaic", "datatype": "byte"}],
            http_json=Mock(return_value={}),
            planet_pages=Mock(return_value=[{"_links": {"download": "https://storage.example/quad"}}]),
            download_file=lambda session, url, output: shutil.copy2(source, output),
        )
        folder = self.root / "visual_output"
        folder.mkdir()
        with self.assertLogs("test_multisource", level="WARNING"):
            metadata = self.ns["pull_planet"](
                self.row, self.grid, folder, "planet_before", self.record["windows"]["before"], "fingerprint",
            )
        self.assertEqual(metadata["band_order"], ["red", "green", "blue", "alpha"])
        self.assertEqual(metadata["product_type"], "visual")
        self.assertFalse(metadata["used_for_ndvi"])
        path = self.ns["raster_path"](folder, self.row, "planet_before")
        with rasterio.open(path) as src:
            self.assertEqual(src.read()[:, 1, 1].tolist(), [30, 50, 70, 255])
            self.assertTrue(np.isnan(src.read()[:, 0, 0]).all())
        with self.assertRaisesRegex(ValueError, "not the NDVI source"):
            self.ns["ndvi_from_raster"](path)

    def test_s2_mask_selects_true_red_nir_and_clear_scl_before_scaling(self):
        scene, scl, clear, spectral, masks, reduced, combined = [Mock() for _ in range(7)]
        scene.select.side_effect = [scl, spectral]
        scl.remap.return_value.eq.return_value = clear
        spectral.mask.return_value = masks
        masks.reduce.return_value = reduced
        clear.And.return_value = combined
        self.ns["ee"].Reducer = SimpleNamespace(min=lambda: "min-reducer")
        output = self.ns["mask_s2_scene"](scene)
        self.assertEqual(scene.select.call_args_list[0].args, ("SCL",))
        self.assertEqual(scene.select.call_args_list[1].args, (["B4", "B8"], ["red", "nir"]))
        scl.remap.assert_called_once_with([4, 5, 6, 7], [1, 1, 1, 1], 0)
        masks.reduce.assert_called_once_with("min-reducer")
        spectral.updateMask.assert_called_once_with(combined)
        spectral.updateMask.return_value.multiply.assert_called_once_with(.0001)
        self.assertIs(output, spectral.updateMask.return_value.multiply.return_value.toFloat.return_value)

    def test_s2_export_provenance_exact_season_and_cached_reuse(self):
        collection = Mock()
        for method in ("filterBounds", "filterDate", "filter", "map"):
            getattr(collection, method).return_value = collection
        collection.size.return_value.getInfo.return_value = 3
        collection.aggregate_array.return_value.getInfo.return_value = ["a", "b", "c"]
        self.ns["ee"].ImageCollection = Mock(return_value=collection)
        self.ns["ee"].Filter = SimpleNamespace(lte=lambda *args: args)
        self.ns["gee_geometry"] = lambda grid: "aoi"
        def fake_export(image, row, grid, folder, layer, fingerprint, metadata, bands):
            data = np.stack([
                np.full((grid["height"], grid["width"]), value, dtype=np.float32) for value in (.2, .6)
            ])
            self.ns["write_grid"](
                self.ns["raster_path"](folder, row, layer), data, grid, fingerprint, metadata, bands,
            )
            return metadata
        self.ns["pull_gee"] = Mock(side_effect=fake_export)
        grid = self.ns["s2_screening_grid"](self.grid)
        folder = self.root / "s2"
        folder.mkdir()
        window = self.record["windows"]["before"]
        info = self.ns["pull_s2_screening"](self.row, grid, folder, "before", window, self.record["windows"])
        collection.filterDate.assert_called_once_with(window["start"], window["end_exclusive"])
        collection.map.assert_called_once_with(self.ns["mask_s2_scene"])
        collection.filter.assert_called_once_with(("CLOUDY_PIXEL_PERCENTAGE", 80))
        self.assertEqual(info["scene_ids"], ["a", "b", "c"])
        self.assertEqual(info["source_bands"], ["B4", "B8"])
        self.assertEqual(info["reflectance_scales"], [1, 1])
        self.ns["pull_s2_screening"](self.row, grid, folder, "before", window, self.record["windows"])
        self.ns["pull_gee"].assert_called_once()

    def test_s2_empty_collection_is_explicitly_missing_without_export(self):
        collection = Mock()
        for method in ("filterBounds", "filterDate", "filter"):
            getattr(collection, method).return_value = collection
        collection.size.return_value.getInfo.return_value = 0
        self.ns["ee"].ImageCollection = Mock(return_value=collection)
        self.ns["ee"].Filter = SimpleNamespace(lte=lambda *args: args)
        self.ns["gee_geometry"] = lambda grid: "aoi"
        self.ns["pull_gee"] = Mock()
        with self.assertRaisesRegex(self.ns["MissingLayer"], "No Sentinel-2 SR scenes"):
            self.ns["pull_s2_screening"](
                self.row, self.ns["s2_screening_grid"](self.grid), self.root,
                "before", self.record["windows"]["before"], self.record["windows"],
            )
        self.ns["pull_gee"].assert_not_called()
        collection.map.assert_not_called()

    def test_s2_missing_data_is_inconclusive_and_original_inputs_can_still_publish(self):
        self.ns["pull_all_layers"] = self.complete_attempt
        self.ns["pull_s2_screening"] = Mock(side_effect=self.ns["MissingLayer"]("No SR scenes"))
        with self.assertLogs("test_multisource", level="WARNING"):
            record = self.ns["process_record"](self.record)
        self.assertEqual(record["status"], "complete")
        self.assertEqual(record["date_used"], "original")
        self.assertEqual(record["ndvi_check_result"], "inconclusive")
        self.assertEqual(record["original_ndvi"]["ndvi_check_result"], "inconclusive")
        self.assertTrue(record["fallback_triggered"])
        self.assertEqual(record["screening_layers"], {"s2_screen_before": "missing", "s2_screen_after": "missing"})
        self.ns["validate_folder"](self.root / "candidates/12_3", record)
        self.assertIn("No SR scenes", record["screening_errors"]["s2_screen_before"])

    def test_screening_patch_threshold_uses_ten_m_not_planet_grid(self):
        self.config["MAX_LOSS_FRACTION"] = 1
        grid = self.ns["s2_screening_grid"](self.grid)
        self.assertEqual((grid["width"], grid["height"]), (100, 100))
        before = np.full((100, 100), .5)
        valid = np.ones(before.shape, dtype=bool)
        for pixels, expected in ((15, "pass"), (16, "fail")):
            after = before.copy()
            after.flat[:pixels] = 0
            stats = self.ns["ndvi_statistics"](before, after, valid, valid, grid["pixel_size_m"])
            self.assertEqual(stats["ndvi_check_result"], expected)
            self.assertEqual(stats["largest_loss_patch_m2"], pixels * 100)

    def test_incomplete_s2_pair_is_inconclusive_and_does_not_read_planet_rgb(self):
        folder = self.root / "partial_screen"
        folder.mkdir()
        result = {
            "layers": {"planet_before": "ok", "planet_after": "ok"},
            "windows": self.record["windows"],
        }
        def partial(row, grid, folder, side, window, windows):
            if side == "after":
                raise self.ns["MissingLayer"]("No S2 after scenes")
            return self.s2_attempt(row, grid, folder, side, window, windows)
        self.ns["pull_s2_screening"] = partial
        self.ns["ndvi_from_raster"] = Mock(side_effect=AssertionError("Must not evaluate incomplete pair"))
        with self.assertLogs("test_multisource", level="ERROR"):
            stats = self.ns["check_pair"](self.row, self.grid, folder, result)
        self.assertEqual(stats["ndvi_check_result"], "inconclusive")
        self.assertEqual(stats["ndvi_source"], "COPERNICUS/S2_SR_HARMONIZED")
        self.assertEqual(result["screening_layers"], {"s2_screen_before": "ok", "s2_screen_after": "missing"})
        self.assertTrue((folder / "12_3_s2_screen_before.tif").exists())
        self.ns["ndvi_from_raster"].assert_not_called()

    def test_gee_export_uses_exact_grid_and_keeps_db_values(self):
        captured = {}
        image = Mock()
        image.select.return_value = image
        image.toFloat.return_value = image
        image.unmask.return_value = image
        def url(params):
            captured.update(params)
            return "https://example.invalid/signed-download"
        image.getDownloadURL.side_effect = url
        def fake_download(session, url, output):
            with rasterio.open(
                output, "w", driver="GTiff", width=333, height=333, count=2,
                dtype="float32", crs="EPSG:32645",
                transform=rasterio.Affine(*self.grid["transform"]),
            ) as dst:
                data = np.array([np.full((333, 333), value, dtype=np.float32) for value in (-10, -15)])
                data[:, 0, 0] = -9999
                dst.write(data)
        self.ns["download_file"] = fake_download
        folder = self.root / "gee_result"
        folder.mkdir()
        self.ns["pull_gee"](
            image, self.row, self.grid, folder, "s1_before",
            "fingerprint", {"units": "dB"}, ["VV", "VH"],
        )
        self.assertEqual(captured["dimensions"], [333, 333])
        self.assertEqual(captured["crs_transform"], list(self.grid["transform"]))
        self.assertEqual(captured["crs"], "EPSG:32645")
        with rasterio.open(folder / "12_3_s1_before.tif") as src:
            self.assertEqual(src.read()[:, 1, 1].tolist(), [-10, -15])
            self.assertTrue(np.isnan(src.read()[:, 0, 0]).all())

    def test_orbit_direction_uses_combined_scene_counts_and_tie_break(self):
        counts = {
            ("ASCENDING", "2017-10-01"): 4, ("ASCENDING", "2019-10-01"): 1,
            ("DESCENDING", "2017-10-01"): 1, ("DESCENDING", "2019-10-01"): 7,
        }
        class Collection:
            def __init__(self, direction=None, start=None):
                self.direction, self.start = direction, start
            def filterBounds(self, geometry):
                return self
            def filter(self, condition):
                if condition[0] == "orbitProperties_pass":
                    return Collection(direction=condition[1], start=self.start)
                return self
            def filterDate(self, start, end):
                return Collection(direction=self.direction, start=start)
            def size(self):
                return SimpleNamespace(getInfo=lambda: counts[(self.direction, self.start)])
        self.ns["ee"] = SimpleNamespace(
            ImageCollection=lambda asset: Collection(),
            Filter=SimpleNamespace(eq=lambda *args: args, listContains=lambda *args: args),
        )
        self.ns["gee_geometry"] = lambda grid: None
        chosen, _, selected = self.ns["s1_collections"](self.grid, self.record["windows"])
        self.assertEqual(chosen, "DESCENDING")
        self.assertEqual(selected, {"before": 1, "after": 7})
        counts[("DESCENDING", "2019-10-01")] = 4
        chosen, _, _ = self.ns["s1_collections"](self.grid, self.record["windows"])
        self.assertEqual(chosen, "ASCENDING")

    def test_ndvi_loss_fraction_and_strict_thresholds(self):
        before = np.ones((10, 10), dtype=np.float32) * .75
        after = before.copy()
        valid = np.ones(before.shape, dtype=bool)
        after.flat[:2] = .25
        stats = self.ns["ndvi_statistics"](before, after, valid, valid, 3)
        self.assertEqual(stats["ndvi_check_result"], "pass")
        self.assertEqual(stats["ndvi_loss_fraction"], .02)
        after.flat[2] = .25
        self.assertEqual(self.ns["ndvi_statistics"](
            before, after, valid, valid, 3,
        )["ndvi_check_result"], "fail")
        after[:] = before - .25
        self.assertEqual(self.ns["ndvi_statistics"](
            before, after, valid, valid, 3,
        )["ndvi_loss_fraction"], 0)

    def test_patch_area_uses_actual_pixel_size_and_eight_connectivity(self):
        self.config.update(MAX_LOSS_FRACTION=1)
        before = np.ones((20, 20), dtype=np.float32) * .75
        valid = np.ones(before.shape, dtype=bool)
        for pixels, expected in ((166, "pass"), (167, "fail")):
            after = before.copy()
            after.flat[:pixels] = 0
            stats = self.ns["ndvi_statistics"](before, after, valid, valid, 1000 / 333)
            self.assertEqual(stats["ndvi_check_result"], expected)
            self.assertEqual(stats["largest_loss_patch_px"], pixels)
        self.config.update(MAX_LOSS_PATCH_AREA_M2=5)
        before = np.ones((2, 2), dtype=np.float32)
        after = np.array([[0, 1], [1, 0]], dtype=np.float32)
        valid = np.ones((2, 2), dtype=bool)
        stats = self.ns["ndvi_statistics"](before, after, valid, valid, 2)
        self.assertEqual(stats["largest_loss_patch_px"], 2)
        self.assertEqual(stats["ndvi_check_result"], "fail")

    def test_insufficient_valid_and_missing_pair_fall_back(self):
        data = np.zeros((10, 10))
        valid = np.ones((10, 10), dtype=bool)
        valid.flat[:31] = False
        result = self.ns["ndvi_statistics"](data, data, valid, valid, 3)
        self.assertEqual(result["ndvi_check_result"], "inconclusive")
        valid[:] = False
        result = self.ns["ndvi_statistics"](data, data, valid, valid, 3)
        self.assertIsNone(result["mean_dndvi"])
        json.dumps(result, allow_nan=False)
        result = self.ns["check_pair"](
            self.row, self.grid, self.root,
            {"layers": {"planet_before": "missing", "planet_after": "ok"}},
        )
        self.assertEqual(result["ndvi_check_result"], "missing_mosaic")

    def test_fallback_reuses_terrain_and_preserves_shifted_statistics(self):
        pull = Mock(side_effect=self.complete_attempt)
        self.ns["pull_all_layers"] = pull
        self.ns["check_pair"] = Mock(side_effect=[
            {"ndvi_check_result": "fail", "ndvi_loss_fraction": .5},
            {"ndvi_check_result": "fail", "ndvi_loss_fraction": .8},
        ])
        record = self.ns["process_record"](self.record)
        self.assertEqual(record["status"], "complete")
        self.assertEqual(record["date_used"], "original")
        self.assertEqual(record["effective_incident_date"], "2024-12-17")
        self.assertEqual(record["ndvi_loss_fraction"], .5)
        self.assertEqual(record["original_ndvi"]["ndvi_loss_fraction"], .8)
        self.assertEqual(pull.call_count, 2)
        self.assertEqual(pull.call_args.kwargs["terrain_cache"], self.root / ".staging/12_3/shifted")
        self.assertTrue((self.root / "candidates/12_3/provenance.json").exists())
        self.assertFalse((self.root / ".staging/12_3").exists())
        self.ns["validate_folder"](self.root / "candidates/12_3", record)

    def test_failed_fallback_does_not_publish_partial_folder(self):
        def partial(row, grid, folder, incident_date, terrain_cache=None):
            result = self.complete_attempt(row, grid, folder, incident_date, terrain_cache)
            if incident_date == "2024-12-17":
                result["layers"]["s1_after"] = "missing"
            return result
        self.ns["pull_all_layers"] = partial
        self.ns["check_pair"] = Mock(return_value={"ndvi_check_result": "inconclusive"})
        result = self.ns["process_record"](self.record)
        self.assertEqual(result["status"], "incomplete")
        self.assertFalse((self.root / "candidates/12_3").exists())
        self.assertTrue((self.root / ".staging/12_3/shifted").exists())

    def test_resume_recovers_published_provenance_without_downloads(self):
        self.publish_complete()
        self.ns["pull_all_layers"] = Mock(side_effect=AssertionError("Must not re-download"))
        result = self.ns["process_record"](self.record)
        self.assertEqual(result["status"], "complete")
        self.ns["pull_all_layers"].assert_not_called()

    def test_interrupted_swap_rolls_back_original_folder(self):
        source = self.root / ".staging/12_3"
        destination = self.root / "candidates/12_3"
        source.mkdir()
        destination.mkdir()
        (destination / "old.txt").write_text("old")
        real_replace = os.replace
        def fail_publish(src, dst):
            if Path(src) == source:
                raise OSError("simulated failure")
            return real_replace(src, dst)
        with patch("os.replace", side_effect=fail_publish):
            with self.assertRaises(OSError):
                self.ns["publish_folder"](source, destination)
        self.assertEqual((destination / "old.txt").read_text(), "old")

    def test_archive_has_matching_csv_count_and_all_six_layers(self):
        self.publish_complete()
        entry = self.ns["create_archive"](["12_3"])
        path = self.root / "archives" / entry["name"]
        self.ns["verify_archive"](path, ["12_3"])
        self.assertFalse(entry["uploaded"])
        self.assertEqual(self.ns["STATE"]["records"]["12_3"]["archive"], entry["name"])
        with zipfile.ZipFile(path) as archive:
            self.assertIn("README.md", archive.namelist())
            self.assertEqual(len([name for name in archive.namelist() if name.endswith(".tif")]), 6)
        with self.assertRaisesRegex(ValueError, "folders"):
            self.ns["verify_archive"](path, ["wrong_key"])

    def test_no_cleanup_before_confirmed_upload_and_remote_size(self):
        self.publish_complete()
        entry = self.ns["create_archive"](["12_3"])
        self.ns["CommitOperationAdd"] = lambda **kwargs: kwargs
        hf = self.ns["HF"]
        hf.create_commit.return_value = SimpleNamespace(oid="commit1")
        hf.get_paths_info.return_value = [SimpleNamespace(size=1)]
        with self.assertRaisesRegex(RuntimeError, "size verification failed"):
            self.ns["upload_archive"](entry)
        self.assertFalse(entry["uploaded"])
        self.assertTrue((self.root / "candidates/12_3").exists())
        self.assertTrue((self.root / "archives" / entry["name"]).exists())
        hf.get_paths_info.return_value = [SimpleNamespace(size=entry["size_bytes"])]
        self.ns["upload_archive"](entry)
        self.assertTrue(entry["uploaded"])
        self.assertTrue(entry["local_cleaned"])
        self.assertFalse((self.root / "candidates/12_3").exists())
        self.assertFalse((self.root / "archives" / entry["name"]).exists())
        self.assertEqual(hf.get_paths_info.call_args.kwargs["revision"], "commit1")

    def test_dry_run_never_calls_hugging_face(self):
        self.publish_complete()
        self.config["DRY_RUN_COUNT"] = 1
        with contextlib.redirect_stdout(io.StringIO()):
            self.ns["run_dataset"]()
        self.ns["HF"].assert_not_called()
        self.ns["HF"].create_repo.assert_not_called()
        self.ns["HF"].create_commit.assert_not_called()
        self.assertEqual(len(self.ns["STATE"]["archives"]), 1)
        self.assertTrue((self.root / "candidates/12_3").exists())

    def test_three_candidate_dry_run_includes_real_ndvi_fallback_and_test_zip(self):
        records = {}
        for index in range(3):
            row = {
                **self.record, "key": f"12_{index}", "candidate_id": str(index),
                "year_shifted": index == 0,
            }
            records[row["key"]] = row
        self.ns["STATE"]["records"] = records
        self.ns["pull_all_layers"] = Mock(side_effect=self.complete_attempt)
        self.ns["pull_s2_screening"] = Mock(side_effect=self.s2_attempt)
        with contextlib.redirect_stdout(io.StringIO()):
            self.ns["run_dataset"]()
        record = records["12_0"]
        self.assertEqual(record["status"], "complete")
        self.assertTrue(record["fallback_triggered"])
        self.assertEqual(record["date_used"], "original")
        self.assertEqual(record["ndvi_check_result"], "fail")
        self.assertEqual(record["ndvi_loss_fraction"], 1)
        self.assertEqual(record["original_ndvi"]["ndvi_check_result"], "pass")
        self.assertEqual(len(self.ns["STATE"]["archives"]), 1)
        self.assertEqual(len(self.ns["STATE"]["archives"][0]["keys"]), 3)
        self.assertEqual(self.ns["pull_all_layers"].call_count, 4)
        self.assertEqual(self.ns["pull_s2_screening"].call_count, 4)
        self.assertEqual(record["ndvi_source"], "COPERNICUS/S2_SR_HARMONIZED")
        self.assertEqual(record["ndvi_resolution_m"], 10)
        self.assertEqual(record["screening_grid"]["width"], 100)
        self.assertEqual(record["screening_metadata"]["s2_screen_before"]["window"], record["windows"]["before"])
        self.assertEqual(record["screening_metadata"]["s2_screen_after"]["window"], record["windows"]["after"])
        archive = self.root / "archives" / self.ns["STATE"]["archives"][0]["name"]
        with zipfile.ZipFile(archive) as handle:
            self.assertIn("12_0/12_0_s2_screen_before.tif", handle.namelist())
            self.assertIn("12_0/12_0_s2_screen_after.tif", handle.namelist())
        # A published candidate cannot silently lose its successful screening evidence.
        (self.root / "candidates/12_0/12_0_s2_screen_before.tif").unlink()
        with self.assertRaises(rasterio.errors.RasterioIOError):
            self.ns["validate_folder"](self.root / "candidates/12_0", record)
        self.ns["HF"].create_repo.assert_not_called()
        self.ns["HF"].create_commit.assert_not_called()

    def test_all_nodata_with_zero_alpha_is_not_a_valid_cached_layer(self):
        folder = self.root / "invalid_alpha"
        folder.mkdir()
        metadata = {"band_order": ["blue", "green", "red", "nir", "alpha"]}
        with self.assertRaisesRegex(ValueError, "No valid data"):
            self.ns["write_grid"](
                folder / "invalid.tif", np.zeros((5, 333, 333), dtype=np.float32),
                self.grid, "fingerprint", metadata, metadata["band_order"],
            )
        self.assertFalse((folder / "invalid.tif").exists())

    def test_public_repo_is_rejected_when_private_required(self):
        self.ns["HF"].repo_info.return_value = SimpleNamespace(private=False)
        with self.assertRaisesRegex(ValueError, "public"):
            self.ns["ensure_hf_repo"]()

    def test_transient_retries_are_bounded_and_permanent_http_errors_fail_fast(self):
        transient = Mock(side_effect=[requests.Timeout(), "ok"])
        self.assertEqual(self.ns["retry_call"](transient, "test"), "ok")
        self.assertEqual(transient.call_count, 2)
        response = requests.Response()
        response.status_code = 403
        permanent = Mock(side_effect=requests.HTTPError(response=response))
        with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
            self.ns["retry_call"](permanent, "test")
        self.assertEqual(permanent.call_count, 1)

    def test_authentication_removes_temporary_key_even_when_gee_initialization_fails(self):
        import sys

        values = {
            "GEE_JSON_KEY": json.dumps({
                "client_email": "test@example.invalid", "project_id": "test",
                "private_key": "test-placeholder-not-a-credential",
            }),
            "PLANET_API_KEY": "test-placeholder", "HF_TOKEN": "test-placeholder",
        }
        client = SimpleNamespace(get_secret=values.__getitem__)
        captured = []
        def credentials(email, path):
            captured.append(Path(path))
            self.assertEqual(Path(path).stat().st_mode & 0o777, 0o600)
            return "test"
        self.ns["ee"] = SimpleNamespace(
            ServiceAccountCredentials=credentials, Initialize=Mock(side_effect=RuntimeError("test")),
        )
        with patch.dict(sys.modules, {"kaggle_secrets": SimpleNamespace(UserSecretsClient=lambda: client)}):
            with self.assertRaisesRegex(RuntimeError, "GEE_JSON_KEY authentication failed"):
                self.ns["authenticate"]()
        self.assertEqual(len(captured), 1)
        self.assertFalse(captured[0].exists())

    def test_calibration_without_labels_skips_with_explicit_warning(self):
        self.config["CALIBRATE_NDVI"] = True
        self.ns["pull_planet"] = Mock(side_effect=AssertionError("Must not download"))
        with self.assertLogs("test_multisource", level="WARNING") as logs:
            self.ns["calibrate_ndvi"](pd.DataFrame([{"key": "1_1"}]))
        self.assertIn("no label column", " ".join(logs.output))
        self.ns["pull_planet"].assert_not_called()

    def test_calibration_negative_only_reports_missing_positive_group_and_stops(self):
        self.config["CALIBRATE_NDVI"] = True
        frame = pd.DataFrame([self.row])
        self.ns["pull_planet"] = Mock(return_value={})
        self.ns["check_pair"] = Mock(return_value={
            "ndvi_check_result": "pass", "ndvi_loss_fraction": .01,
            "largest_loss_patch_px": 1,
        })
        figure, axes = Mock(), [Mock(), Mock()]
        self.ns["plt"] = Mock()
        self.ns["plt"].subplots.return_value = (figure, axes)
        with self.assertLogs("test_multisource", level="WARNING") as logs:
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, "Calibration finished"):
                    self.ns["calibrate_ndvi"](frame)
        self.assertIn("No labeled 2017", " ".join(logs.output))
        self.assertEqual(self.ns["pull_planet"].call_count, 2)
        self.assertTrue((self.root / "ndvi_calibration.csv").exists())
        figure.savefig.assert_called_once()
        self.assertFalse((self.root / ".calibration/12_3").exists())

    def test_run_signature_prevents_stale_config_resume(self):
        csv = self.root / "input.csv"
        pd.DataFrame([
            {key: value for key, value in self.row.items()
             if key in ("incident_id", "candidate_id", "west", "south", "east", "north")}
            | {"incident_date": "2024-12-17"},
        ]).to_csv(csv, index=False)
        self.config.update(INPUT_CSV=str(csv), OUTPUT_DIR=str(self.root), DRY_RUN=False)
        with contextlib.redirect_stdout(io.StringIO()):
            root, _, state, signature = self.ns["prepare_run"]()
        self.ns.update(ROOT=root, STATE=state, RUN_SIGNATURE=signature)
        self.ns["checkpoint"]()
        self.config["NDVI_DROP_THRESHOLD"] = .3
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, "another configuration"):
                self.ns["prepare_run"]()

    def test_deletion_guard_rejects_root(self):
        with self.assertRaisesRegex(ValueError, "Refusing deletion"):
            self.ns["remove_child_tree"](self.root, self.root)
        self.assertTrue(self.root.exists())

    def test_candidate_processing_overlaps_but_state_and_archives_stay_on_main_thread(self):
        self.config.update(MAX_WORKERS=2, BATCH_SIZE=3)
        records = {f"12_{i}": {**deepcopy(self.record), "key": f"12_{i}"} for i in range(6)}
        self.ns["STATE"]["records"] = records
        main_id = threading.get_ident()
        lock = threading.Lock()
        barrier = threading.Barrier(2)
        active = 0
        peak = 0
        calls = []
        flushed = []
        def process(record):
            nonlocal active, peak
            self.assertNotEqual(threading.get_ident(), main_id)
            self.assertIsNot(record, records[record["key"]])
            self.assertIsNot(record["grid"], records[record["key"]]["grid"])
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                if record["key"] in ("12_0", "12_1"):
                    barrier.wait(timeout=5)
                time.sleep(.01)
                record["layers"]["planet_before"] = "worker-mutated"
                if record["key"] == "12_1":
                    raise ValueError("simulated candidate failure")
                return {**record, "status": "complete"}
            finally:
                with lock:
                    active -= 1
        def checkpoint():
            calls.append(threading.get_ident())
        def flush(keys):
            self.assertEqual(active, 0)
            self.assertEqual(threading.get_ident(), main_id)
            self.assertLessEqual(len(keys), 3)
            flushed.extend(keys)
        self.ns.update(
            process_record=process, checkpoint=checkpoint, flush_batch=flush,
            folder_bytes=lambda key: 100, report_run=Mock(),
        )
        with contextlib.redirect_stdout(io.StringIO()), self.assertLogs("test_multisource", level="ERROR"):
            self.ns["run_dataset"]()
        self.assertEqual(peak, 2)
        self.assertEqual(set(calls), {main_id})
        self.assertEqual(records["12_1"]["status"], "failed")
        self.assertEqual(records["12_1"]["layers"]["planet_before"], "pending")
        self.assertEqual(set(flushed), set(records) - {"12_1"})
        self.assertEqual(len(flushed), 5)
        self.ns["HF"].create_commit.assert_not_called()
        self.ns["HF"].create_repo.assert_not_called()

    def test_worker_http_sessions_are_isolated_and_closed_on_success_and_failure(self):
        self.ns["PLANET"] = SimpleNamespace(auth=("test-placeholder", ""), headers={"User-Agent": "test"})
        sessions = []
        lock = threading.Lock()
        barrier = threading.Barrier(2)
        def factory():
            session = SimpleNamespace(auth=None, headers={}, close=Mock())
            with lock:
                sessions.append(session)
            return session
        def process(record):
            session = self.ns["planet_session"]()
            barrier.wait(timeout=5)
            self.assertIs(session, self.ns["planet_session"]())
            self.assertEqual(session.auth, self.ns["PLANET"].auth)
            self.assertEqual(session.headers, self.ns["PLANET"].headers)
            record["grid"]["width"] = 1
            if record["key"] == "bad":
                raise ValueError("test")
            return record
        self.ns["process_record"] = process
        with patch.object(requests, "Session", side_effect=factory):
            with ThreadPoolExecutor(max_workers=2) as executor:
                good = executor.submit(self.ns["process_candidate_worker"], self.record)
                bad = executor.submit(self.ns["process_candidate_worker"], {**self.record, "key": "bad"})
                self.assertEqual(good.result(timeout=5)["grid"]["width"], 1)
                with self.assertRaisesRegex(ValueError, "test"):
                    bad.result(timeout=5)
        self.assertEqual(len(sessions), 2)
        self.assertIsNot(sessions[0], sessions[1])
        for session in sessions:
            session.close.assert_called_once()
        self.assertEqual(self.record["grid"]["width"], 333)
        self.assertIs(self.ns["planet_session"](), self.ns["PLANET"])

    def test_disk_headroom_reduces_worker_concurrency(self):
        self.config.update(MAX_WORKERS=4, BATCH_SIZE=10, DOWNLOAD_WORKSPACE_GB=1)
        self.ns["STATE"]["records"] = {
            f"12_{i}": {**deepcopy(self.record), "key": f"12_{i}"} for i in range(3)
        }
        lock = threading.Lock()
        active = 0
        peak = 0
        def process(record):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(active, peak)
            time.sleep(.01)
            with lock:
                active -= 1
            return {**record, "status": "complete"}
        self.ns.update(
            process_record=process, disk_free=lambda path: int(1.5 * 1024 ** 3),
            folder_bytes=lambda key: 100, checkpoint=Mock(), flush_batch=Mock(), report_run=Mock(),
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.ns["run_dataset"]()
        self.assertEqual(peak, 1)
        self.assertEqual(self.ns["flush_batch"].call_args.args[0], ["12_0", "12_1", "12_2"])

    def test_no_disk_headroom_stops_before_submitting_downloads(self):
        self.config.update(DOWNLOAD_WORKSPACE_GB=1)
        self.ns.update(process_record=Mock(), disk_free=lambda path: 0)
        with self.assertRaisesRegex(OSError, "headroom"):
            self.ns["run_dataset"]()
        self.ns["process_record"].assert_not_called()

    def test_worker_count_changes_do_not_invalidate_resume_signature(self):
        csv = self.root / "input.csv"
        pd.DataFrame([{
            "incident": 1, "candidate": 2, "incident_date": "2024-01-01",
            "min_lon": 85, "max_lon": 86, "min_lat": 27, "max_lat": 28,
        }]).to_csv(csv, index=False)
        self.config.update(INPUT_CSV=str(csv), OUTPUT_DIR=str(self.root), DRY_RUN=False, MAX_WORKERS=1)
        with contextlib.redirect_stdout(io.StringIO()):
            _, _, state, signature = self.ns["prepare_run"]()
        self.ns["atomic_json"](self.root / "run_state.json", state)
        self.config["MAX_WORKERS"] = 4
        with contextlib.redirect_stdout(io.StringIO()):
            _, _, recovered, new_signature = self.ns["prepare_run"]()
        self.assertEqual(new_signature, signature)
        self.assertEqual(recovered["signature"], signature)


if __name__ == "__main__":
    unittest.main()
