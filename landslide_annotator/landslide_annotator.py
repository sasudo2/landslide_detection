import os
import csv
import glob
from datetime import datetime

from qgis.PyQt.QtWidgets import (
    QAction, QApplication, QDialog, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QFileDialog, QMessageBox,
    QSplitter, QProgressBar, QGroupBox, QGridLayout
)
from qgis.PyQt.QtCore import Qt, QSettings
from qgis.PyQt.QtGui import QColor

from qgis.core import (
    QgsRasterLayer, QgsContrastEnhancement,
    QgsCoordinateTransform, QgsProject, QgsCoordinateReferenceSystem,
    QgsPointXY, QgsColorRampShader, QgsSingleBandPseudoColorRenderer
)
from qgis.gui import QgsMapCanvas


class LandslideAnnotatorPlugin:

    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dialog = None

    def initGui(self):
        self.action = QAction("Landslide Annotator", self.iface.mainWindow())
        self.action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu("&Landslide Annotator", self.action)

    def unload(self):
        self.iface.removeToolBarIcon(self.action)
        self.iface.removePluginMenu("&Landslide Annotator", self.action)
        if self.dialog:
            self.dialog.close()

    def run(self):
        if not self.dialog:
            self.dialog = AnnotationDialog(self.iface)
        self.dialog.show()
        self.dialog.raise_()


class AnnotationDialog(QDialog):

    def __init__(self, iface):
        super().__init__(iface.mainWindow())
        self.iface = iface
        self.setWindowTitle("Landslide Annotator")
        self.resize(1200, 800)

        self.items = []
        self.current_idx = -1
        self.csv_path = ""
        self.annotations = {}
        self.annotations_ts = {}
        self.centroids = {}
        self.before_layer = None
        self.after_layer = None
        self.slope_layer = None
        self.mask_layer = None
        self._setup_ui()
        self._load_from_settings()

    def _setup_ui(self):
        layout = QVBoxLayout()

        settings_group = QGroupBox("Input Settings")
        settings_grid = QGridLayout()

        settings_grid.addWidget(QLabel("Base Directory:"), 0, 0)
        self.base_dir_edit = QLineEdit()
        self.base_dir_edit.setPlaceholderText("/path/to/incident_folders")
        settings_grid.addWidget(self.base_dir_edit, 0, 1)

        self.browse_base_btn = QPushButton("Browse...")
        self.browse_base_btn.clicked.connect(self._browse_base_dir)
        settings_grid.addWidget(self.browse_base_btn, 0, 2)

        self.scan_btn = QPushButton("Scan & Start")
        self.scan_btn.setStyleSheet("font-weight: bold; padding: 6px;")
        self.scan_btn.clicked.connect(self._scan_and_start)
        settings_grid.addWidget(self.scan_btn, 0, 3)

        settings_grid.addWidget(QLabel("Output CSV:"), 1, 0)
        self.csv_edit = QLineEdit()
        self.csv_edit.setPlaceholderText("landslide_annotations.csv")
        settings_grid.addWidget(self.csv_edit, 1, 1)

        self.browse_csv_btn = QPushButton("Browse...")
        self.browse_csv_btn.clicked.connect(self._browse_csv)
        settings_grid.addWidget(self.browse_csv_btn, 1, 2)

        settings_group.setLayout(settings_grid)
        layout.addWidget(settings_group)

        map_group = QGroupBox("Map Views")
        map_layout = QVBoxLayout()

        label_row = QHBoxLayout()
        before_label = QLabel("BEFORE")
        before_label.setAlignment(Qt.AlignCenter)
        before_label.setStyleSheet("font-weight: bold; font-size: 13px;")
        after_label = QLabel("AFTER")
        after_label.setAlignment(Qt.AlignCenter)
        after_label.setStyleSheet("font-weight: bold; font-size: 13px;")
        slope_label = QLabel("SLOPE")
        slope_label.setAlignment(Qt.AlignCenter)
        slope_label.setStyleSheet("font-weight: bold; font-size: 13px;")
        mask_label = QLabel("MASK")
        mask_label.setAlignment(Qt.AlignCenter)
        mask_label.setStyleSheet("font-weight: bold; font-size: 13px;")
        label_row.addWidget(before_label)
        label_row.addWidget(after_label)
        label_row.addWidget(slope_label)
        label_row.addWidget(mask_label)
        map_layout.addLayout(label_row)

        splitter = QSplitter(Qt.Horizontal)
        self.before_canvas = QgsMapCanvas()
        self.before_canvas.setCanvasColor(Qt.black)
        self.before_canvas.setContextMenuPolicy(Qt.CustomContextMenu)
        self.before_canvas.customContextMenuRequested.connect(
            lambda pos: self._copy_coords(pos, self.before_canvas))
        self.after_canvas = QgsMapCanvas()
        self.after_canvas.setCanvasColor(Qt.black)
        self.after_canvas.setContextMenuPolicy(Qt.CustomContextMenu)
        self.after_canvas.customContextMenuRequested.connect(
            lambda pos: self._copy_coords(pos, self.after_canvas))
        self.slope_canvas = QgsMapCanvas()
        self.slope_canvas.setCanvasColor(Qt.black)
        self.slope_canvas.setContextMenuPolicy(Qt.CustomContextMenu)
        self.slope_canvas.customContextMenuRequested.connect(
            lambda pos: self._copy_coords(pos, self.slope_canvas))
        self.mask_canvas = QgsMapCanvas()
        self.mask_canvas.setCanvasColor(Qt.black)
        self.mask_canvas.setContextMenuPolicy(Qt.CustomContextMenu)
        self.mask_canvas.customContextMenuRequested.connect(
            lambda pos: self._copy_coords(pos, self.mask_canvas))
        splitter.addWidget(self.before_canvas)
        splitter.addWidget(self.after_canvas)
        splitter.addWidget(self.slope_canvas)
        splitter.addWidget(self.mask_canvas)
        map_layout.addWidget(splitter, stretch=1)

        map_group.setLayout(map_layout)
        layout.addWidget(map_group, stretch=1)

        nav_group = QGroupBox("Navigation")
        nav_layout = QHBoxLayout()

        self.first_btn = QPushButton("|\u25C0 First")
        self.first_btn.clicked.connect(self._first)
        nav_layout.addWidget(self.first_btn)

        self.prev_btn = QPushButton("\u25C0 Back")
        self.prev_btn.clicked.connect(self._prev)
        nav_layout.addWidget(self.prev_btn)

        self.info_label = QLabel("No data loaded")
        self.info_label.setAlignment(Qt.AlignCenter)
        nav_layout.addWidget(self.info_label, stretch=1)

        self.next_btn = QPushButton("Next \u25B6")
        self.next_btn.clicked.connect(self._next)
        nav_layout.addWidget(self.next_btn)

        self.last_btn = QPushButton("Last \u25B6|")
        self.last_btn.clicked.connect(self._last)
        nav_layout.addWidget(self.last_btn)

        nav_group.setLayout(nav_layout)
        layout.addWidget(nav_group)

        self.progress_bar = QProgressBar()
        layout.addWidget(self.progress_bar)

        annot_group = QGroupBox("Annotation")
        annot_layout = QHBoxLayout()

        self.not_landslide_btn = QPushButton("Not Landslide")
        self.not_landslide_btn.setStyleSheet(
            "background-color: #ffcccc; padding: 12px; font-size: 13px;"
        )
        self.not_landslide_btn.clicked.connect(self._mark_not_landslide)
        annot_layout.addWidget(self.not_landslide_btn, stretch=1)

        self.skip_btn = QPushButton("Skip")
        self.skip_btn.setStyleSheet("padding: 12px; font-size: 13px;")
        self.skip_btn.clicked.connect(self._skip)
        annot_layout.addWidget(self.skip_btn)

        self.landslide_btn = QPushButton("YES \u2714 Landslide")
        self.landslide_btn.setStyleSheet(
            "background-color: #ccffcc; padding: 12px; font-size: 13px; font-weight: bold;"
        )
        self.landslide_btn.clicked.connect(self._mark_landslide)
        annot_layout.addWidget(self.landslide_btn, stretch=1)

        annot_group.setLayout(annot_layout)
        layout.addWidget(annot_group)

        self.status_label = QLabel("Ready \u2014 select base directory and scan")
        layout.addWidget(self.status_label)

        self.setLayout(layout)

    def _browse_base_dir(self):
        path = QFileDialog.getExistingDirectory(self, "Select Base Directory with incident_* folders")
        if path:
            self.base_dir_edit.setText(path)

    def _browse_csv(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Output CSV File", "", "CSV Files (*.csv);;All Files (*)"
        )
        if path:
            self.csv_edit.setText(path)

    def _get_raster_bounds(self, raster_path):
        temp_layer = QgsRasterLayer(raster_path, "temp", "gdal")
        if not temp_layer.isValid():
            return None, None, None, None

        extent = temp_layer.extent()

        min_x = extent.xMinimum()
        max_x = extent.xMaximum()
        min_y = extent.yMinimum()
        max_y = extent.yMaximum()

        layer_crs = temp_layer.crs()
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")

        if layer_crs != wgs84 and layer_crs.isValid() and wgs84.isValid():
            transform = QgsCoordinateTransform(
                layer_crs,
                wgs84,
                QgsProject.instance().transformContext()
            )

            bottom_left = transform.transform(QgsPointXY(min_x, min_y))
            top_right = transform.transform(QgsPointXY(max_x, max_y))

            return (
                round(bottom_left.y(), 6),   # min_lat
                round(top_right.y(), 6),     # max_lat
                round(bottom_left.x(), 6),   # min_lon
                round(top_right.x(), 6)      # max_lon
            )

        return (
            round(min_y, 6),
            round(max_y, 6),
            round(min_x, 6),
            round(max_x, 6)
        )

    def _find_raster_by_keyword(self, tif_files, keyword):
        """Return the first raster whose filename contains the given token
        (split on underscores), e.g. 'before', 'after', 'mask'."""
        for f in tif_files:
            stem = os.path.splitext(os.path.basename(f))[0].lower()
            if keyword in stem.split('_'):
                return f
        return None

    def _scan_and_start(self):
        base_dir = self.base_dir_edit.text().strip()
        csv_path = self.csv_edit.text().strip()

        if not base_dir:
            QMessageBox.warning(self, "Error", "Please select a base directory containing incident_* folders.")
            return

        if not os.path.isdir(base_dir):
            QMessageBox.warning(self, "Error", f"Directory does not exist:\n{base_dir}")
            return

        if not csv_path:
            csv_path = os.path.join(base_dir, "landslide_annotations.csv")
            self.csv_edit.setText(csv_path)
        self.csv_path = csv_path

        incident_dirs = sorted(glob.glob(os.path.join(base_dir, "incident_*")))
        if not incident_dirs:
            QMessageBox.warning(self, "Error",
                f"No incident_* folders found in:\n{base_dir}")
            return

        self.items = []
        self.centroids = {}
        for incident_dir in incident_dirs:
            incident_id = os.path.basename(incident_dir).replace("incident_", "")
            candidate_dirs = sorted(glob.glob(os.path.join(incident_dir, "candidate_*")))
            for candidate_dir in candidate_dirs:
                candidate_id = os.path.basename(candidate_dir)
                tif_files = sorted(
                    glob.glob(os.path.join(candidate_dir, "*.tif")) +
                    glob.glob(os.path.join(candidate_dir, "*.tiff"))
                )

                before = self._find_raster_by_keyword(tif_files, "before")
                after = self._find_raster_by_keyword(tif_files, "after")
                slope = self._find_raster_by_keyword(tif_files, "slope")
                mask = self._find_raster_by_keyword(tif_files, "mask")

                if before and after:
                    self.items.append(
                        (incident_id, candidate_id, before, after, slope, mask))
                    bounds = self._get_raster_bounds(after)
                    self.centroids[(incident_id, candidate_id)] = bounds

        if not self.items:
            QMessageBox.warning(self, "Error",
                "No candidate pairs found. Expected structure:\n"
                "  incident_XXXX/candidate_YYYY/*.tif\n"
                "Filenames must contain the 'before' and 'after' tokens\n"
                "(e.g. incident_123_candidate_1_before.tif / _after.tif).")
            return

        self._load_annotations()
        self.current_idx = -1
        self._update_save_button_states()
        self._navigate(0)

        self.status_label.setText(
            f"Loaded {len(self.items)} candidates from {len(incident_dirs)} incidents. "
            f"Previously annotated: {len(self.annotations)}"
        )

    def _load_annotations(self):
        self.annotations = {}
        self.annotations_ts = {}
        if os.path.exists(self.csv_path):
            try:
                with open(self.csv_path, 'r') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        inc = row.get('incident', '').strip() or row.get('incident_id', '').strip()
                        cand = row.get('candidate', '').strip() or row.get('candidate_id', '').strip()
                        key = (inc, cand)
                        self.annotations[key] = row.get('label', 'landslide').strip()
                        self.annotations_ts[key] = row.get('timestamp', '').strip()
            except Exception:
                pass

    def _navigate(self, idx):
        if idx < 0 or idx >= len(self.items):
            return

        self.current_idx = idx
        incident_id, candidate_id, before_path, after_path, slope_path, mask_path = self.items[idx]

        self.before_canvas.setLayers([])
        self.after_canvas.setLayers([])
        self.slope_canvas.setLayers([])
        self.mask_canvas.setLayers([])

        self.before_layer = QgsRasterLayer(before_path, "Before", "gdal")
        if self.before_layer.isValid():
            self._stretch_raster(self.before_layer)
            self.before_canvas.setLayers([self.before_layer])
        else:
            QMessageBox.warning(self, "Load Error",
                f"Cannot load before image:\n{before_path}\n\n"
                f"The file may be corrupt, inaccessible, or not a valid GeoTIFF.")

        self.after_layer = QgsRasterLayer(after_path, "After", "gdal")
        if self.after_layer.isValid():
            self._stretch_raster(self.after_layer)
            self.after_canvas.setLayers([self.after_layer])
        else:
            QMessageBox.warning(self, "Load Error",
                f"Cannot load after image:\n{after_path}\n\n"
                f"The file may be corrupt, inaccessible, or not a valid GeoTIFF.")

        self.slope_layer = None
        if slope_path:
            self.slope_layer = QgsRasterLayer(slope_path, "Slope", "gdal")
            if self.slope_layer.isValid():
                self._stretch_raster(self.slope_layer)
                self.slope_canvas.setLayers([self.slope_layer])
            else:
                QMessageBox.warning(self, "Load Error",
                    f"Cannot load slope image:\n{slope_path}\n\n"
                    f"The file may be corrupt, inaccessible, or not a valid GeoTIFF.")

        self.mask_layer = None
        if mask_path:
            self.mask_layer = QgsRasterLayer(mask_path, "Mask", "gdal")
            if self.mask_layer.isValid():
                self._style_mask_layer(self.mask_layer)
                self.mask_canvas.setLayers([self.mask_layer])
            else:
                QMessageBox.warning(self, "Load Error",
                    f"Cannot load mask image:\n{mask_path}\n\n"
                    f"The file may be corrupt, inaccessible, or not a valid GeoTIFF.")

        self.before_canvas.zoomToFullExtent()
        self.after_canvas.zoomToFullExtent()
        self.slope_canvas.zoomToFullExtent()
        self.mask_canvas.zoomToFullExtent()
        self.before_canvas.refresh()
        self.after_canvas.refresh()
        self.slope_canvas.refresh()
        self.mask_canvas.refresh()

        self._update_ui()

    def _update_ui(self):
        if self.current_idx < 0 or self.current_idx >= len(self.items):
            return
        incident_id, candidate_id, *_ = self.items[self.current_idx]
        key = (incident_id, candidate_id)

        total = len(self.items)
        viewed = len(self.annotations)
        is_annotated = key in self.annotations
        label = self.annotations.get(key, "")

        self.info_label.setText(
            f"{incident_id} / {candidate_id}  "
            f"[{self.current_idx + 1} of {total}]"
        )

        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(self.current_idx + 1)

        self.first_btn.setEnabled(self.current_idx > 0)
        self.prev_btn.setEnabled(self.current_idx > 0)
        self.next_btn.setEnabled(self.current_idx < total - 1)
        self.last_btn.setEnabled(self.current_idx < total - 1)

        annot_status = ""
        if is_annotated:
            if label == 'landslide':
                annot_status = "Previously: LANDSLIDE"
                self.landslide_btn.setStyleSheet(
                    "background-color: #66ff66; padding: 12px; font-size: 13px; font-weight: bold; border: 2px solid #009900;"
                )
                self.not_landslide_btn.setStyleSheet(
                    "background-color: #ffcccc; padding: 12px; font-size: 13px;"
                )
            else:
                annot_status = "Previously: NOT landslide"
                self.landslide_btn.setStyleSheet(
                    "background-color: #ccffcc; padding: 12px; font-size: 13px; font-weight: bold;"
                )
                self.not_landslide_btn.setStyleSheet(
                    "background-color: #ff6666; padding: 12px; font-size: 13px; border: 2px solid #990000;"
                )
        else:
            self.landslide_btn.setStyleSheet(
                "background-color: #ccffcc; padding: 12px; font-size: 13px; font-weight: bold;"
            )
            self.not_landslide_btn.setStyleSheet(
                "background-color: #ffcccc; padding: 12px; font-size: 13px;"
            )

        self.status_label.setText(
            f"Viewing: {incident_id}/{candidate_id}  |  "
            f"Annotated: {viewed}/{total}  "
            f"{'[' + annot_status + ']' if annot_status else '[Not yet annotated]'}"
        )

    def _update_save_button_states(self):
        has_data = len(self.items) > 0
        self.first_btn.setEnabled(has_data)
        self.prev_btn.setEnabled(has_data)
        self.next_btn.setEnabled(has_data)
        self.last_btn.setEnabled(has_data)
        self.landslide_btn.setEnabled(has_data)
        self.not_landslide_btn.setEnabled(has_data)
        self.skip_btn.setEnabled(has_data)

    def _stretch_raster(self, layer):
        renderer = layer.renderer()
        if renderer and hasattr(renderer, 'setContrastEnhancementAlgorithm'):
            try:
                renderer.setContrastEnhancementAlgorithm(
                    QgsContrastEnhancement.StretchToMinimumMaximum
                )
            except Exception:
                pass

    def _cleanup_temp_composites(self):
        """Placeholder for cleanup (no temp files created in native QGIS version)."""
        pass

    def _style_mask_layer(self, layer):
        """Render a binary mask (0/1 uint8) as a transparent background with the
        '1' pixels shown in solid red, so the candidate region is clearly
        visible. This avoids the common QGIS pitfall where a 0/1 image is
        stretched against the full 0-255 uint8 range and renders all-black.
        """
        try:
            provider = layer.dataProvider()
            shader = QgsColorRampShader()
            shader.setColorRampType(QgsColorRampShader.Discrete)
            items = [
                QgsColorRampShader.ColorRampItem(0, QColor(0, 0, 0, 0), "no change"),
                QgsColorRampShader.ColorRampItem(1, QColor(255, 0, 0, 255), "candidate"),
            ]
            shader.setColorRampItemList(items)
            renderer = QgsSingleBandPseudoColorRenderer(provider, 1, shader)
            layer.setRenderer(renderer)
        except Exception:
            # Fall back to a forced 0-1 contrast stretch if styling fails
            try:
                ce = QgsContrastEnhancement(layer.dataProvider().dataType(1))
                ce.setContrastEnhancementAlgorithm(
                    QgsContrastEnhancement.StretchToMinimumMaximum)
                ce.setMinimumValue(0)
                ce.setMaximumValue(1)
                layer.renderer().setContrastEnhancement(ce)
            except Exception:
                pass

    def _copy_coords(self, pos, canvas):
        layer = None
        if canvas is self.before_canvas:
            layer = self.before_layer
        elif canvas is self.after_canvas:
            layer = self.after_layer
        if not layer or not layer.isValid():
            return
        try:
            map_point = canvas.getCoordinateTransform().toMapCoordinates(pos.x(), pos.y())
            canvas_crs = canvas.mapSettings().destinationCrs()
            if not canvas_crs.isValid():
                return
            wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
            coord_transform = QgsCoordinateTransform(
                canvas_crs, wgs84, QgsProject.instance().transformContext()
            )
            wgs84_point = coord_transform.transform(map_point)
            text = f"{wgs84_point.y():.6f}, {wgs84_point.x():.6f}"
            QApplication.clipboard().setText(text)
            self.status_label.setText(f"Copied: {text}")
        except Exception:
            pass

    def _mark_landslide(self):
        if self.current_idx < 0:
            return
        incident_id, candidate_id, *_ = self.items[self.current_idx]
        self._save_annotation(incident_id, candidate_id, 'landslide')
        self.status_label.setText(f"Marked as LANDSLIDE: {incident_id}/{candidate_id}")
        self._advance()

    def _mark_not_landslide(self):
        if self.current_idx < 0:
            return
        incident_id, candidate_id, *_ = self.items[self.current_idx]
        self._save_annotation(incident_id, candidate_id, 'not_landslide')
        self.status_label.setText(f"Marked as NOT landslide: {incident_id}/{candidate_id}")
        self._advance()

    def _skip(self):
        if self.current_idx < 0:
            return
        incident_id, candidate_id, *_ = self.items[self.current_idx]
        self.status_label.setText(f"Skipped: {incident_id}/{candidate_id}")
        self._advance()

    def _save_annotation(self, incident_id, candidate_id, label):
        key = (incident_id, candidate_id)
        self.annotations[key] = label
        self.annotations_ts[key] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        try:
            os.makedirs(os.path.dirname(self.csv_path) or '.', exist_ok=True)
            with open(self.csv_path, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                                'incident',
                                'candidate',
                                'min_lat',
                                'max_lat',
                                'min_lon',
                                'max_lon',
                                'label',
                                'timestamp'
                            ])
                for (inc_id, cand_id), lbl in sorted(self.annotations.items()):
                    ts = self.annotations_ts.get((inc_id, cand_id), '')
                    min_lat, max_lat, min_lon, max_lon = self.centroids.get(
                        (inc_id, cand_id),
                        (None, None, None, None)
                    )

                    writer.writerow([
                        inc_id,
                        cand_id,
                        min_lat if min_lat is not None else '',
                        max_lat if max_lat is not None else '',
                        min_lon if min_lon is not None else '',
                        max_lon if max_lon is not None else '',
                        lbl,
                        ts
                    ])
        except Exception as e:
            QMessageBox.critical(self, "CSV Error", f"Failed to write CSV:\n{e}")

        self._update_ui()

    def _advance(self):
        if self.current_idx < len(self.items) - 1:
            self._navigate(self.current_idx + 1)

    def _first(self):
        if self.items:
            self._navigate(0)

    def _last(self):
        if self.items:
            self._navigate(len(self.items) - 1)

    def _prev(self):
        self._navigate(self.current_idx - 1)

    def _next(self):
        self._navigate(self.current_idx + 1)

    def _save_to_settings(self):
        settings = QSettings()
        settings.setValue("landslide_annotator/last_base_dir", self.base_dir_edit.text())
        settings.setValue("landslide_annotator/last_csv", self.csv_edit.text())

    def _load_from_settings(self):
        settings = QSettings()
        base_dir = settings.value("landslide_annotator/last_base_dir", "")
        csv_path = settings.value("landslide_annotator/last_csv", "")
        if base_dir:
            self.base_dir_edit.setText(base_dir)
        if csv_path:
            self.csv_edit.setText(csv_path)

    def closeEvent(self, event):
        self._save_to_settings()
        super().closeEvent(event)
