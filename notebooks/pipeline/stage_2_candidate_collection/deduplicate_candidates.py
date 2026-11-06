#!/usr/bin/env python3
"""Find spatially repeating candidate folders and optionally delete duplicates."""

import argparse
import csv
import math
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import rasterio
from rasterio.warp import transform_bounds


@dataclass(frozen=True)
class Candidate:
    folder: Path
    bounds: tuple[float, float, float, float]


def validate_bounds(bounds):
    left, bottom, right, top = bounds
    if (
        not all(math.isfinite(value) for value in bounds)
        or not -180 <= left < right <= 180
        or not -90 < bottom < top < 90
        or right - left > 180
    ):
        raise ValueError(f"Invalid or unsupported geographic bounds: {bounds}")
    return bounds


def iou(first, second):
    left, bottom, right, top = first
    other_left, other_bottom, other_right, other_top = second
    intersection = max(0, min(right, other_right) - max(left, other_left)) * max(
        0, min(top, other_top) - max(bottom, other_bottom)
    )
    union = (
        (right - left) * (top - bottom)
        + (other_right - other_left) * (other_top - other_bottom)
        - intersection
    )
    return intersection / union if union > 0 else 0.0


def numbered_folders(parent, prefix):
    folders = []
    for path in parent.iterdir():
        match = re.fullmatch(rf"{prefix}_(\d+)", path.name)
        if match:
            if path.is_symlink() or not path.is_dir():
                raise ValueError(f"Expected a real directory, not a symlink or file: {path}")
            folders.append((int(match[1]), path.name, path))
    return [path for _, _, path in sorted(folders)]


def scan_candidates(root):
    candidates = []
    for incident in numbered_folders(root, "incident"):
        for folder in numbered_folders(incident, "candidate"):
            rasters = [
                path for path in folder.iterdir()
                if path.suffix.lower() in (".tif", ".tiff")
                and "after" in path.stem.lower().split("_")
            ]
            if len(rasters) != 1 or rasters[0].is_symlink():
                raise ValueError(f"Expected exactly one non-symlink after raster in {folder}")
            with rasterio.open(rasters[0]) as src:
                if src.crs is None:
                    raise ValueError(f"Raster has no CRS: {rasters[0]}")
                bounds = transform_bounds(src.crs, "EPSG:4326", *src.bounds)
            candidates.append(Candidate(folder, validate_bounds(bounds)))
    if not candidates:
        raise ValueError(f"No candidate folders found under {root}/incident_*/candidate_*")
    return candidates


def find_duplicates(candidates, threshold):
    kept, duplicates = [], []
    for candidate in candidates:
        for representative in kept:
            overlap = iou(candidate.bounds, representative.bounds)
            if overlap >= threshold:
                duplicates.append((candidate, representative, overlap))
                break
        else:
            kept.append(candidate)
    return kept, duplicates


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path, help="one local variant folder containing incident_* folders")
    parser.add_argument("--iou", type=float, default=0.50, help="duplicate overlap threshold (default: 0.50)")
    parser.add_argument("--delete", action="store_true", help="permanently delete duplicate candidate folders")
    parser.add_argument("--report", type=Path, help="CSV report; default: <folder>/candidate_dedup_report.csv")
    args = parser.parse_args(argv)
    if not math.isfinite(args.iou) or not 0 < args.iou <= 1:
        parser.error("--iou must be in (0, 1]")
    if args.folder.is_symlink():
        parser.error("folder must not be a symlink")
    root = args.folder.resolve(strict=True)
    candidates = scan_candidates(root)
    kept, duplicates = find_duplicates(candidates, args.iou)
    report = args.report if args.report is not None else root / "candidate_dedup_report.csv"
    if report.is_symlink():
        parser.error("report must not be a symlink")
    report = report.resolve()
    if any(report == item.folder or item.folder in report.parents for item in candidates):
        parser.error("report must be outside all candidate folders")
    with report.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["duplicate_folder", "kept_folder", "iou", "mode"])
        for duplicate, representative, overlap in duplicates:
            writer.writerow([
                str(duplicate.folder.relative_to(root)),
                str(representative.folder.relative_to(root)),
                overlap, "delete_requested" if args.delete else "preview",
            ])
    print(f"Scanned {len(candidates)} candidates; keep {len(kept)}, duplicates {len(duplicates)}.")
    print(f"Report: {report}", flush=True)
    if args.delete:
        for duplicate, _, _ in duplicates:
            folder = duplicate.folder
            if folder.is_symlink() or folder.resolve() != folder or folder.parent.parent != root:
                raise ValueError(f"Candidate path changed or escaped selected root: {folder}")
            shutil.rmtree(folder)
            print(f"Deleted {folder}", flush=True)
    else:
        print("Preview only. Rerun with --delete to permanently remove duplicate candidate folders.")
    print(
        "Matching ignores incident dates and labels. Study metadata and annotations are unchanged; "
        "use a separate annotation copy, not a canonical study folder. "
        "Rerunning the downloader may restore deleted candidates."
    )


if __name__ == "__main__":
    main()
