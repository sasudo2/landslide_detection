#!/usr/bin/env bash
set -euo pipefail

python3 - "$@" <<'PY'
import argparse
import filecmp
import os
import re
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath


parser = argparse.ArgumentParser(
    prog="download_ablation_candidates.sh",
    description="Download candidate chips and run metadata for one independent ablation variant.",
    epilog="Indices are 1-based positions among numerically sorted incidents with candidates, not incident IDs.",
)
parser.add_argument("variant", choices=(
    "baseline", "optical_only", "sar_only", "no_slope", "no_slic", "no_bonferroni",
))
parser.add_argument("start", nargs="?", type=int, help="first incident index (inclusive)")
parser.add_argument("end", nargs="?", type=int, help="last incident index (inclusive)")
parser.add_argument("--study-root", default="ablations/study_01")
parser.add_argument("--repo", default="sasudo2/landslides", help="Hugging Face dataset repository")
parser.add_argument("--revision", default="main", help="branch, tag, or commit SHA")
parser.add_argument("--outdir", type=Path, help="destination; defaults to ./ablation_candidates/<study-root>/<variant>")
args = parser.parse_args()

root = PurePosixPath(args.study_root)
if (
    not args.study_root or root.is_absolute() or str(root) != args.study_root
    or any(part in (".", "..") or part.startswith(".") for part in root.parts)
    or "\\" in args.study_root
):
    parser.error("--study-root must be a safe relative folder, such as ablations/study_01")
if args.start is not None and args.start < 1:
    parser.error("start must be at least 1")
if args.end is not None and args.end < args.start:
    parser.error("end must be greater than or equal to start")

from huggingface_hub import HfApi, hf_hub_download
from huggingface_hub.errors import HfHubHTTPError, LocalEntryNotFoundError


def retry_hub(action, description):
    for attempt in range(1, 6):
        try:
            return action()
        except (HfHubHTTPError, LocalEntryNotFoundError, ConnectionError, TimeoutError) as exc:
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)
            transient = (
                status in (408, 429, 500, 502, 503, 504)
                or isinstance(exc, (LocalEntryNotFoundError, ConnectionError, TimeoutError))
            )
            if not transient or attempt == 5:
                raise
            delay = min(2 ** attempt, 60)
            retry_after = response.headers.get("Retry-After") if response is not None else None
            if retry_after:
                try:
                    delay = max(delay, float(retry_after))
                except ValueError:
                    pass
            print(f"{description} failed: {exc}; retry {attempt}/5 in {delay:.0f}s", flush=True)
            time.sleep(delay)


os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "0"
api = HfApi(token=os.environ.get("HF_TOKEN") or None)
info = retry_hub(
    lambda: api.dataset_info(args.repo, revision=args.revision),
    "Resolve dataset revision",
)
sha = info.sha
if not isinstance(sha, str) or re.fullmatch(r"[0-9a-fA-F]{40}", sha) is None:
    raise RuntimeError(f"Dataset returned an invalid immutable commit SHA: {sha!r}")

prefix = f"{args.study_root}/{args.variant}/"
entries = retry_hub(
    lambda: list(api.list_repo_tree(
        repo_id=args.repo, repo_type="dataset", revision=sha,
        path_in_repo=prefix.rstrip("/"), recursive=True,
    )),
    "List selected variant",
)
files = set()
for entry in entries:
    path = getattr(entry, "path", "")
    if not hasattr(entry, "blob_id"):
        continue
    if (
        not path.startswith(prefix) or str(PurePosixPath(path)) != path
        or "\\" in path or any(part in (".", "..") for part in PurePosixPath(path).parts)
    ):
        raise RuntimeError(f"Unexpected path in variant listing: {path!r}")
    files.add(path)
if not files:
    raise RuntimeError(f"No files found for {args.repo}@{sha}/{prefix}")

candidate_pattern = re.compile(
    rf"^{re.escape(prefix)}incident_(\d+)/candidate_\d+/[^/]+\.tif$"
)
available_ids = sorted({
    match.group(1) for path in files if (match := candidate_pattern.fullmatch(path))
}, key=int)
if args.start is not None:
    if args.start > len(available_ids):
        parser.error(f"start is out of range; {len(available_ids)} incident(s) have candidates")
    end = args.end if args.end is not None else args.start
    if end > len(available_ids):
        print(f"Warning: clamping end {end} to {len(available_ids)}")
    selected_ids = available_ids[args.start - 1:min(end, len(available_ids))]
else:
    selected_ids = available_ids

metadata_names = {
    "study.json", "run.json", "summary.json", "candidate_status.csv",
    "candidate_metadata.csv", "diagnostics.json",
}
selected_prefixes = tuple(prefix + f"incident_{incident_id}/" for incident_id in selected_ids)
selected_files = sorted(
    path for path in files
    if path[len(prefix):] in metadata_names
    or (selected_prefixes and path.startswith(selected_prefixes))
)
if not selected_files:
    raise RuntimeError(f"No candidate or run metadata files found under {prefix}")

outdir = args.outdir if args.outdir is not None else Path("ablation_candidates") / root / args.variant
print(f"Pinned {args.repo}@{args.revision} -> {sha}", flush=True)
print(
    f"Downloading {args.variant}: {len(selected_ids)}/{len(available_ids)} incident(s) with candidates "
    f"and run metadata -> {outdir}",
    flush=True,
)


def download_file(remote):
    source = Path(retry_hub(
        lambda: hf_hub_download(
            repo_id=args.repo, repo_type="dataset", revision=sha,
            token=os.environ.get("HF_TOKEN") or None, filename=remote,
        ),
        f"Download {remote}",
    ))
    if not source.is_file():
        raise FileNotFoundError(f"Cache is missing requested file: {remote}")
    return remote, source


copied = skipped = 0
with ThreadPoolExecutor(max_workers=8) as executor:
    for completed, (remote, source) in enumerate(executor.map(download_file, selected_files), 1):
        destination = outdir / remote[len(prefix):]
        if destination.is_file() and filecmp.cmp(source, destination, shallow=False):
            skipped += 1
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".ablation-download-", dir=destination.parent)
            os.close(fd)
            try:
                shutil.copyfile(source, temporary)
                os.replace(temporary, destination)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            copied += 1
        if completed == 1 or completed % 25 == 0 or completed == len(selected_files):
            print(f"Processed {completed}/{len(selected_files)} file(s)", flush=True)

print(f"Copied {copied} file(s); {skipped} unchanged file(s) already present.")
print("Run metadata describes the full variant, even when an incident range is selected.")
PY
