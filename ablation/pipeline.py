"""Notebook-derived IR-MAD detection with independently switchable components."""

import argparse
import csv
import hashlib
import importlib.metadata
import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

import numpy as np
import rasterio
from huggingface_hub import HfApi, hf_hub_download
from rasterio.transform import xy
from rasterio.warp import (
    Resampling,
    calculate_default_transform,
    reproject,
    transform_bounds,
)
from rasterio.windows import Window, transform as window_transform
from scipy.stats import chi2, norm
from skimage.filters import apply_hysteresis_threshold
from skimage.measure import label, regionprops
from skimage.morphology import remove_small_objects
from skimage.segmentation import slic

LOG = logging.getLogger(__name__)
TARGET_CRS = "EPSG:4326"
M_PER_DEG_LAT = 111320.0
RAW_ROOT = "raw_images/raw_incidents"
STATUS_FIELDS = ["incident_id", "status", "n_candidates", "error"]
META_FIELDS = [
    "incident_id", "candidate_id", "area_m2", "elongation", "severity", "score",
    "lon_min", "lat_min", "lon_max", "lat_max",
]
PACKAGES = ("numpy", "scipy", "rasterio", "scikit-image", "huggingface_hub")


@dataclass(frozen=True)
class Settings:
    scale_m: float = 10
    min_slope_deg: float = 20
    target_superpixel_m: float = 30
    high_percentile: float = 99.7
    low_percentile: float = 93.0
    alpha: float = 0.05
    max_iters: int = 20
    min_blob_area_m2: float = 3000
    max_blob_area_m2: float = 2000000
    max_elongation: float = 5
    max_candidates: int = 100
    chip_pad_m: float = 300


@dataclass(frozen=True)
class Variant:
    modality: str = "fused"
    slope: bool = True
    slic: bool = True
    bonferroni: bool = True


VARIANTS = {
    "baseline": Variant(),
    "optical_only": Variant(modality="optical"),
    "sar_only": Variant(modality="sar"),
    "no_slope": Variant(slope=False),
    "no_slic": Variant(slic=False),
    "no_bonferroni": Variant(bonferroni=False),
}


def norm01(x):
    a, b = np.nanmin(x), np.nanmax(x)
    if not np.isfinite(a) or not np.isfinite(b) or b <= a:
        return np.zeros_like(x, dtype=np.float32)
    return ((x - a) / (b - a)).astype(np.float32)


def wmean_cov(x, weights):
    weights = weights / np.sum(weights)
    mean = np.sum(x * weights[:, None], axis=0)
    centered = x - mean
    return mean, (centered * weights[:, None]).T @ centered


def cca_axes(sxx, syy, sxy, reg=1e-6):
    lx = np.linalg.cholesky(sxx + reg * np.eye(sxx.shape[0]))
    ly = np.linalg.cholesky(syy + reg * np.eye(syy.shape[0]))
    inv_lx, inv_ly = np.linalg.inv(lx), np.linalg.inv(ly)
    u, _, vt = np.linalg.svd(inv_lx @ sxy @ inv_ly.T, full_matrices=False)
    return inv_lx.T @ u, inv_ly.T @ vt.T


def irmad(x, y, max_iters=20, tol=1e-3):
    n, p = x.shape
    weights = np.ones(n, dtype=np.float64)
    previous = None
    chi = None
    for _ in range(max_iters):
        mx, sxx = wmean_cov(x, weights)
        my, syy = wmean_cov(y, weights)
        xc, yc = x - mx, y - my
        sxy = (xc * (weights / np.sum(weights))[:, None]).T @ yc
        a, b = cca_axes(sxx, syy, sxy)
        mad = xc @ a[:, :p] - yc @ b[:, :p]
        std = np.std(mad, axis=0) + 1e-6
        chi = np.sum((mad / std[None, :]) ** 2, axis=1)
        no_change_p = 1.0 - chi2.cdf(chi, df=p)
        if previous is not None and np.mean(np.abs(no_change_p - previous)) < tol:
            break
        previous = no_change_p
        weights = np.clip(no_change_p, 1e-6, 1.0)
    if chi is None:
        raise ValueError("IR-MAD requires at least one iteration")
    return chi, chi2.cdf(chi, df=p)


def build_target_grid(ref_path, scale_m=10):
    with rasterio.open(ref_path) as src:
        _, bottom, _, top = transform_bounds(src.crs, TARGET_CRS, *src.bounds)
        mean_lat_rad = np.deg2rad((bottom + top) / 2)
        m_per_deg_lon = M_PER_DEG_LAT * max(np.cos(mean_lat_rad), 1e-6)
        transform, width, height = calculate_default_transform(
            src.crs, TARGET_CRS, src.width, src.height, *src.bounds,
            resolution=(scale_m / m_per_deg_lon, scale_m / M_PER_DEG_LAT),
        )
    if width is None or height is None:
        raise ValueError(f"Could not determine analysis-grid dimensions for {ref_path}")
    return transform, int(width), int(height), mean_lat_rad


def reproject_bands(path, indexes, transform, shape):
    out = np.zeros((len(indexes), *shape), dtype=np.float32)
    with rasterio.open(path) as src:
        for i, index in enumerate(indexes):
            reproject(
                source=rasterio.band(src, index), destination=out[i],
                src_transform=src.transform, src_crs=src.crs,
                dst_transform=transform, dst_crs=TARGET_CRS,
                resampling=Resampling.bilinear,
            )
    if not np.all(np.isfinite(out)):
        raise ValueError(f"Non-finite raster values in {path}")
    return out


def select_rgb_band_indexes(path):
    with rasterio.open(path) as src:
        if src.count >= 4:
            band = src.read(4)
            values = band[np.isfinite(band)]
            alpha = False
            if values.size >= 100:
                lo, hi = np.percentile(values, [1, 99])
                alpha = hi - lo < 2 or len(np.unique(values)) <= 8
            return [1, 2, 3] if alpha else [3, 2, 1]
        if src.count >= 3:
            return [1, 2, 3]
        raise ValueError(f"Optical raster has too few bands ({src.count}) in {path}")


def change_evidence(before, after, optical, settings):
    bands, h, w = before.shape
    valid = (
        np.all(before > 0, axis=0) & np.all(after > 0, axis=0)
        if optical else
        np.all(before != 0, axis=0) & np.all(after != 0, axis=0)
    )
    if np.count_nonzero(valid) <= 100:
        raise ValueError("Selected modality has <=100 valid paired pixels; cannot fit IR-MAD")
    x = np.moveaxis(before, 0, -1)[valid]
    y = np.moveaxis(after, 0, -1)[valid]
    chi_values, confidence_values = irmad(x, y, max_iters=settings.max_iters)
    if not np.all(np.isfinite(chi_values)):
        raise ValueError("IR-MAD produced non-finite change evidence")
    confidence = np.zeros((h, w), dtype=np.float32)
    chi = np.zeros((h, w), dtype=np.float64)
    df = np.zeros((h, w), dtype=np.float64)
    confidence[valid], chi[valid], df[valid] = confidence_values, chi_values, bands
    return confidence, chi, df


def segmentation_features(confidence, post, slope, variant):
    # SAR-only must not leak optical evidence through segmentation.
    channels = [confidence[..., None]]
    channels.append(np.stack([norm01(band) for band in post], axis=-1))
    if variant.slope:
        channels.append(norm01(slope)[..., None])
    return np.concatenate(channels, axis=-1)


def aggregate_segments(confidence, chi, df, terrain, segments):
    ids, inverse = np.unique(segments, return_inverse=True)
    inverse = inverse.reshape(segments.shape)
    active = inverse[terrain]
    counts = np.bincount(active, minlength=len(ids))
    conf_sum = np.bincount(active, weights=confidence[terrain], minlength=len(ids))
    chi_sum = np.bincount(active, weights=chi[terrain], minlength=len(ids))
    df_sum = np.bincount(active, weights=df[terrain], minlength=len(ids))
    conf_mean = np.divide(conf_sum, counts, out=np.zeros(len(ids)), where=counts > 0)
    z = np.divide(
        chi_sum - df_sum, np.sqrt(2 * df_sum),
        out=np.zeros(len(ids)), where=df_sum > 0,
    )
    return conf_mean[inverse].astype(np.float32), z[inverse].astype(np.float32), len(ids)


def detection_mask(confidence, chi, df, terrain, features, px_area, variant, settings):
    if variant.slic:
        pixels_per_segment = max(1, int(settings.target_superpixel_m ** 2 / max(px_area, 1e-6)))
        n_segments = max(50, int(confidence.size / pixels_per_segment))
        segments = slic(
            features, n_segments=n_segments, compactness=0.2,
            start_label=1, channel_axis=-1,
        )
        seg_conf, z, n_tests = aggregate_segments(confidence, chi, df, terrain, segments)
    else:
        seg_conf = confidence
        z = np.divide(chi - df, np.sqrt(2 * df), out=np.zeros_like(chi), where=df > 0)
        n_tests = max(confidence.size, 1)
    alpha = settings.alpha / n_tests if variant.bonferroni else settings.alpha
    z_crit = norm.ppf(1 - alpha)
    valid_conf = seg_conf[terrain]
    if valid_conf.size == 0 or np.max(valid_conf) <= 0:
        return np.zeros_like(terrain, dtype=bool), n_tests, float(z_crit)
    high = np.percentile(valid_conf, settings.high_percentile)
    low = min(np.percentile(valid_conf, settings.low_percentile), high)
    mask = apply_hysteresis_threshold(seg_conf, low, high)
    mask &= terrain & (z >= z_crit)
    return remove_small_objects(mask, min_size=5), n_tests, float(z_crit)


def detect(paths, variant, settings):
    # All variants use the same optical-reference geometry, never its values for SAR.
    transform, w, h, latitude = build_target_grid(paths["before"], settings.scale_m)
    shape = (h, w)
    slope = reproject_bands(paths["slope"], [1], transform, shape)[0]
    evidence = []
    post_features = None
    if variant.modality in ("optical", "fused"):
        before = reproject_bands(paths["before"], select_rgb_band_indexes(paths["before"]), transform, shape)
        after = reproject_bands(paths["after"], select_rgb_band_indexes(paths["after"]), transform, shape)
        evidence.append(change_evidence(before, after, True, settings))
        post_features = after
    if variant.modality in ("sar", "fused"):
        pre = reproject_bands(paths["sar_pre"], [1, 2], transform, shape)
        post = reproject_bands(paths["sar_post"], [1, 2], transform, shape)
        evidence.append(change_evidence(pre, post, False, settings))
        if variant.modality == "sar":
            post_features = post
    if post_features is None:
        raise ValueError(f"Unknown modality: {variant.modality}")
    confidence = np.power(
        np.prod(np.stack([item[0] for item in evidence]), axis=0), 1 / len(evidence),
    ).astype(np.float32)
    chi = np.sum([item[1] for item in evidence], axis=0)
    df = np.sum([item[2] for item in evidence], axis=0)
    terrain = slope >= settings.min_slope_deg if variant.slope else np.ones(shape, dtype=bool)
    confidence *= terrain
    features = segmentation_features(confidence, post_features, slope, variant) if variant.slic else None
    m_per_deg_lon = M_PER_DEG_LAT * max(np.cos(latitude), 1e-6)
    px_area = abs(transform.a * transform.e) * m_per_deg_lon * M_PER_DEG_LAT
    mask, n_tests, z_crit = detection_mask(
        confidence, chi, df, terrain, features, px_area, variant, settings,
    )
    candidates = []
    for region in regionprops(label(mask), intensity_image=confidence):
        area = region.area * px_area
        if not settings.min_blob_area_m2 <= area <= settings.max_blob_area_m2:
            continue
        elongation = (region.major_axis_length or 1) / max(region.minor_axis_length or 1, 1e-6)
        if elongation > settings.max_elongation:
            continue
        r0, c0, r1, c1 = region.bbox
        lon_min, lat_max = xy(transform, r0, c0, offset="ul")
        lon_max, lat_min = xy(transform, r1, c1, offset="ul")
        severity = float(region.mean_intensity)
        candidates.append({
            "area_m2": float(area), "elongation": float(elongation),
            "severity": severity, "score": float(area * severity),
            "lon_min": float(lon_min), "lat_min": float(lat_min),
            "lon_max": float(lon_max), "lat_max": float(lat_max),
            "bbox_px": [int(r0), int(c0), int(r1), int(c1)],
        })
    candidates.sort(key=lambda c: c["score"], reverse=True)
    return {
        "candidates": candidates[:settings.max_candidates], "mask": mask,
        "slope": slope, "transform": transform, "px_area": px_area,
        "n_tests": n_tests, "z_crit": z_crit,
    }


def crop_native(path, indexes, bbox):
    with rasterio.open(path) as src:
        left, bottom, right, top = transform_bounds(TARGET_CRS, src.crs, *bbox)
        win = src.window(left, bottom, right, top)
        c0, r0 = max(0, int(np.floor(win.col_off))), max(0, int(np.floor(win.row_off)))
        c1 = min(src.width, int(np.ceil(win.col_off + win.width)))
        r1 = min(src.height, int(np.ceil(win.row_off + win.height)))
        if c1 <= c0 or r1 <= r0:
            raise ValueError(f"Candidate chip does not overlap {path}")
        win = Window.from_slices((r0, r1), (c0, c1))
        return src.read(indexes, window=win).astype(np.float32), src.window_transform(win), src.crs


def write_chip(path, array, transform, crs):
    data = array[None, ...] if array.ndim == 2 else array
    with rasterio.open(
        path, "w", driver="GTiff", dtype="float32", count=data.shape[0],
        height=data.shape[1], width=data.shape[2], crs=crs, transform=transform,
    ) as dst:
        dst.write(data.astype(np.float32))


def export_candidates(incident_id, paths, result, folder, settings):
    h, w = result["mask"].shape
    transform = result["transform"]
    pad = max(1, int(round(settings.chip_pad_m / max(np.sqrt(result["px_area"]), 1e-6))))
    rows = []
    for candidate_id, candidate in enumerate(result["candidates"], 1):
        row = {key: value for key, value in candidate.items() if key != "bbox_px"}
        row.update(incident_id=incident_id, candidate_id=candidate_id)
        r0, c0, r1, c1 = candidate["bbox_px"]
        r0, c0, r1, c1 = max(0, r0 - pad), max(0, c0 - pad), min(h, r1 + pad), min(w, c1 + pad)
        chip_tf = window_transform(Window.from_slices((r0, r1), (c0, c1)), transform)
        left, top = chip_tf * (0, 0)
        right, bottom = chip_tf * (c1 - c0, r1 - r0)
        bbox = (left, bottom, right, top)
        chip_dir = folder / f"candidate_{candidate_id}"
        chip_dir.mkdir(parents=True)
        specs = {
            "before": crop_native(paths["before"], select_rgb_band_indexes(paths["before"]), bbox),
            "after": crop_native(paths["after"], select_rgb_band_indexes(paths["after"]), bbox),
            "slope": (result["slope"][r0:r1, c0:c1], chip_tf, TARGET_CRS),
            "mask": (result["mask"][r0:r1, c0:c1], chip_tf, TARGET_CRS),
        }
        for name, (array, tf, crs) in specs.items():
            write_chip(chip_dir / f"incident_{incident_id}_candidate_{candidate_id}_{name}.tif", array, tf, crs)
        rows.append(row)
    return rows


def complete_incidents(files):
    grouped = {}
    pattern = re.compile(rf"^{RAW_ROOT}/incident_(\d+)/incident_\1_(.+)$")
    for filename in files:
        match = pattern.match(filename)
        if match:
            grouped.setdefault(int(match[1]), set()).add(match[2])
    mandatory = {"after.tif", "slope.tif", "sar_pre.tif", "sar_post.tif"}
    return sorted(
        incident_id for incident_id, suffixes in grouped.items()
        if mandatory <= suffixes and {"planet_before.tif", "gee_before.tif"} & suffixes
    )


def resolve_hf_token():
    token = os.environ.get("HF_TOKEN")
    if token:
        return token
    try:
        from kaggle_secrets import UserSecretsClient
    except ImportError:
        return None
    try:
        return UserSecretsClient().get_secret("huggingface_token")
    except Exception as exc:
        raise RuntimeError(
            "Attach the Kaggle secret 'huggingface_token' (write access) to this notebook"
        ) from exc


def fetch_paths(repo, revision, incident_id, files, token=None):
    prefix = f"{RAW_ROOT}/incident_{incident_id}/incident_{incident_id}_"
    before = "planet_before" if f"{prefix}planet_before.tif" in files else "gee_before"
    return {
        key: hf_hub_download(
            repo_id=repo, repo_type="dataset", revision=revision,
            filename=f"{prefix}{suffix}.tif", token=token,
        )
        for key, suffix in {
            "before": before, "after": "after", "slope": "slope",
            "sar_pre": "sar_pre", "sar_post": "sar_post",
        }.items()
    }


def validate_study_root(value):
    path = PurePosixPath(value)
    if (
        not path.parts or path.is_absolute() or value != path.as_posix()
        or any(part in (".", "..") or part.startswith(".") for part in path.parts)
        or path.parts[0] in ("candidates", "raw_images")
    ):
        raise argparse.ArgumentTypeError("Use a new relative folder, e.g. ablations/study_01")
    return value


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_csv(path, fields, rows):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def prepare_study(api, args, settings):
    manifest_path = f"{args.study_root}/study.json"
    output_files = set(api.list_repo_files(
        args.output_repo, repo_type="dataset", revision=args.output_revision,
    ))
    identity = {
        "raw_repo": args.repo, "requested_raw_revision": args.raw_revision,
        "requested_incident_ids": sorted(args.incident_ids) if args.incident_ids else None,
        "limit": args.limit, "range": args.range, "settings": asdict(settings),
        "pipeline_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "package_versions": {package: importlib.metadata.version(package) for package in PACKAGES},
    }
    local_study = args.output_dir / args.study_root
    local_study.mkdir(parents=True, exist_ok=True)
    if manifest_path in output_files:
        downloaded = hf_hub_download(
            args.output_repo, manifest_path, repo_type="dataset", revision=args.output_revision,
            token=api.token,
        )
        study = json.loads(Path(downloaded).read_text(encoding="utf-8"))
        if any(study.get(key) != value for key, value in identity.items()):
            raise ValueError("Existing study has different inputs/settings/code; use a new --study-root")
        raw_files = set(api.list_repo_files(
            args.repo, repo_type="dataset", revision=study["raw_revision"],
        ))
    else:
        if any(f.startswith(f"{args.study_root}/") for f in output_files):
            raise ValueError("Study folder already contains unrecognized outputs; use a new --study-root")
        revision = api.repo_info(args.repo, repo_type="dataset", revision=args.raw_revision).sha
        if not revision:
            raise ValueError("Could not resolve raw revision to a commit")
        raw_files = set(api.list_repo_files(args.repo, repo_type="dataset", revision=revision))
        incident_ids = complete_incidents(raw_files)
        if args.incident_ids:
            missing = set(args.incident_ids) - set(incident_ids)
            if missing:
                raise ValueError(f"Requested incidents lack complete inputs: {sorted(missing)}")
            incident_ids = sorted(set(args.incident_ids))
        if args.range:
            start, end = args.range
            if end > len(incident_ids):
                raise ValueError(
                    f"--range end {end} exceeds the {len(incident_ids)} complete incidents available"
                )
            incident_ids = incident_ids[start - 1:end]
        if args.limit:
            incident_ids = incident_ids[:args.limit]
        if not incident_ids:
            raise ValueError("No incidents with complete optical, SAR, and slope inputs")
        study = {**identity, "raw_revision": revision, "incident_ids": incident_ids}
        write_json(local_study / "study.json", study)
        api.upload_file(
            repo_id=args.output_repo, repo_type="dataset", revision=args.output_revision,
            path_or_fileobj=str(local_study / "study.json"), path_in_repo=manifest_path,
            commit_message=f"Initialize ablation study {args.study_root}",
        )
    write_json(local_study / "study.json", study)
    return study, raw_files, output_files, local_study


def run_variant(api, args, study, files, local_study, name, settings):
    variant = VARIANTS[name]
    folder = local_study / name
    if folder.exists():
        raise FileExistsError(f"Local variant folder already exists: {folder}")
    folder.mkdir()
    write_json(folder / "run.json", {
        "variant": name, "switches": asdict(variant),
        "study": study,
    })
    remote = f"{args.study_root}/{name}"
    # Reserve the variant before uploading chips, including zero-candidate runs.
    api.upload_file(
        repo_id=args.output_repo, repo_type="dataset", revision=args.output_revision,
        path_or_fileobj=str(folder / "run.json"), path_in_repo=f"{remote}/run.json",
        commit_message=f"Start ablation {name}",
    )
    records, metadata, diagnostics = [], [], []
    for incident_id in study["incident_ids"]:
        record = {"incident_id": incident_id, "status": "ok", "n_candidates": 0, "error": ""}
        try:
            paths = fetch_paths(args.repo, study["raw_revision"], incident_id, files, api.token)
            result = detect(paths, variant, settings)
            incident_folder = folder / f"incident_{incident_id}"
            incident_folder.mkdir()
            rows = export_candidates(incident_id, paths, result, incident_folder, settings)
            record["n_candidates"] = len(rows)
            diagnostics.append({
                "incident_id": incident_id, "n_tests": result["n_tests"],
                "z_crit": result["z_crit"], "mask_pixels": int(np.count_nonzero(result["mask"])),
            })
            if rows:
                api.upload_folder(
                    repo_id=args.output_repo, repo_type="dataset", revision=args.output_revision,
                    folder_path=str(incident_folder), path_in_repo=f"{remote}/incident_{incident_id}",
                    commit_message=f"Ablation {name}: incident {incident_id}",
                )
            metadata.extend(rows)
            LOG.info("%s incident_%s: %s candidates", name, incident_id, len(rows))
        except Exception as exc:
            LOG.exception("%s incident_%s failed", name, incident_id)
            record.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        records.append(record)
    write_csv(folder / "candidate_status.csv", STATUS_FIELDS, records)
    write_csv(folder / "candidate_metadata.csv", META_FIELDS, metadata)
    write_json(folder / "diagnostics.json", diagnostics)
    failed = sum(record["status"] == "failed" for record in records)
    summary = {
        "variant": name, "n_incidents": len(records), "n_failed": failed,
        "n_ok": len(records) - failed,
        "n_candidates": sum(r["n_candidates"] for r in records if r["status"] == "ok"),
        "n_incidents_with_candidates": sum(r["status"] == "ok" and r["n_candidates"] > 0 for r in records),
    }
    write_json(folder / "summary.json", summary)
    api.upload_folder(
        repo_id=args.output_repo, repo_type="dataset", revision=args.output_revision,
        folder_path=str(folder), path_in_repo=remote,
        allow_patterns=["*.csv", "*.json"],
        commit_message=f"Summarize ablation {name}",
    )
    return summary


def main(preset=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="sasudo2/landslides", help="Raw Hugging Face dataset repo")
    parser.add_argument("--raw-revision", default="main")
    parser.add_argument("--output-repo", help="Existing destination dataset repo (default: --repo)")
    parser.add_argument("--output-revision", default="main", help="Existing destination branch")
    parser.add_argument("--study-root", required=True, type=validate_study_root)
    parser.add_argument("--output-dir", type=Path, default=Path("ablation_results"))
    cohort = parser.add_mutually_exclusive_group()
    cohort.add_argument("--incident-ids", nargs="+", type=int)
    cohort.add_argument(
        "--range", nargs=2, type=int, metavar=("START", "END"),
        help="START-th to END-th complete incident sorted by ID (1-based, inclusive)",
    )
    parser.add_argument("--limit", type=int, help="First N complete incidents, sorted by ID")
    if preset is None:
        parser.add_argument("--variant", choices=[*VARIANTS, "all"], default="all")
    args = parser.parse_args()
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be positive")
    if args.range and not 1 <= args.range[0] <= args.range[1]:
        parser.error("--range needs 1 <= START <= END")
    args.output_repo = args.output_repo or args.repo
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    api, settings = HfApi(token=resolve_hf_token()), Settings()
    study, files, output_files, local_study = prepare_study(api, args, settings)
    selected = preset or args.variant
    names = list(VARIANTS) if selected == "all" else [selected]
    for name in names:
        prefix = f"{args.study_root}/{name}/"
        if any(filename.startswith(prefix) for filename in output_files):
            raise FileExistsError(f"Remote variant already exists: {prefix}; use a new --study-root")
        if (local_study / name).exists():
            raise FileExistsError(f"Local variant already exists: {local_study / name}")
    summaries = [
        run_variant(api, args, study, files, local_study, name, settings)
        for name in names
    ]
    if selected == "all":
        comparison = local_study / "comparison.csv"
        write_csv(comparison, list(summaries[0]), summaries)
        api.upload_file(
            repo_id=args.output_repo, repo_type="dataset", revision=args.output_revision,
            path_or_fileobj=str(comparison), path_in_repo=f"{args.study_root}/comparison.csv",
            commit_message=f"Compare six ablations in {args.study_root}",
        )
    for summary in summaries:
        LOG.info("%s", json.dumps(summary, sort_keys=True))
    if any(summary["n_failed"] for summary in summaries):
        raise SystemExit("Some incidents failed; inspect candidate_status.csv before comparing runs")


if __name__ == "__main__":
    main()
