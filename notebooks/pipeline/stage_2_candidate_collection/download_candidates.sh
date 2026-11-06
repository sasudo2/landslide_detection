#!/usr/bin/env bash
set -euo pipefail

# Download per-candidate chip outputs (and summary CSVs) produced by
# candidate_detection.ipynb from HuggingFace (repo: sasudo2/landslides).
#
# Remote layout (see landslide_detection/README.md):
#   candidates/candidate_status.csv
#   candidates/candidate_metadata.csv
#   candidates/incident_{ID}/candidate_{N}/incident_{ID}_candidate_{N}_{before,after,slope,mask}.tif
#
# Downloaded locally (candidates/ prefix stripped, matching the folder-pair layout
# expected by plugins/landslide_annotator):
#   $OUTDIR/incident_{ID}/candidate_{N}/incident_{ID}_candidate_{N}_{before,after,slope,mask}.tif
#   $OUTDIR/candidate_status.csv
#   $OUTDIR/candidate_metadata.csv
#
# Uses huggingface_hub.snapshot_download() with allow_patterns so the whole
# candidates/ tree (or just the selected incidents) is fetched in one managed call
# instead of one explicit download per file. Downloads reuse the HF cache, so a
# re-run does NOT re-request files that are already cached.
#
# A download manifest (.downloaded_candidates.log) is kept OUTSIDE $OUTDIR (in this
# script's directory by default, or $CANDIDATE_MANIFEST_DIR) so re-runs skip copying
# files that were already placed into $OUTDIR, even if $OUTDIR is cleaned/gitignored.
#
# Index range (1-based, inclusive):
#   200 300       -> download the 200th to 300th incidents in sorted order
# Usage:
#   ./notebooks/pipeline/stage_2_candidate_collection/download_candidates.sh                     # ALL incidents -> ./incidents
#   ./notebooks/pipeline/stage_2_candidate_collection/download_candidates.sh 200                 # 200th incident -> ./incidents
#   ./notebooks/pipeline/stage_2_candidate_collection/download_candidates.sh 200 300             # 200th to 300th -> ./incidents
#   ./notebooks/pipeline/stage_2_candidate_collection/download_candidates.sh 200 300 ./data      # 200th to 300th -> ./data
#   ./notebooks/pipeline/stage_2_candidate_collection/download_candidates.sh ./data              # all -> ./data

TOKEN="${HF_TOKEN:-}"
REPO="sasudo2/landslides"
OUTDIR="./incidents"
RANGE_ARGS=()

for ARG in "$@"; do
    if [[ "$ARG" =~ ^[0-9]+$ ]]; then
        RANGE_ARGS+=("$ARG")
    else
        OUTDIR="$ARG"
    fi
done

echo "Downloading candidate chips from $REPO -> $OUTDIR"
mkdir -p "$OUTDIR"

# Directory that holds the download manifest. Kept OUTSIDE OUTDIR so it survives
# gitignore/cleanup of the (ignored) candidate data folder. Defaults to this script's dir.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST_DIR="${CANDIDATE_MANIFEST_DIR:-$SCRIPT_DIR}"

python3 - "$REPO" "$OUTDIR" "$TOKEN" "$MANIFEST_DIR" "${RANGE_ARGS[@]+"${RANGE_ARGS[@]}"}" << 'EOF'
import os
import re
import shutil
import sys
import time
from urllib.error import HTTPError, URLError

from huggingface_hub import HfApi, snapshot_download
from huggingface_hub.errors import HfHubHTTPError, LocalEntryNotFoundError

# ---------------------------------------------------------------------------
# Whole-folder download via snapshot_download(), with quota-safe behaviour.
#
# HF free accounts are limited to 1000 API requests / 5 minutes. snapshot_download
# still performs one request per file under the hood, so we:
#   1. Disable hf_transfer (fewer requests/connections per file).
#   2. Keep concurrency low (SNAPSHOT_WORKERS).
#   3. Retry the WHOLE snapshot on HTTP 429 with backoff, honouring Retry-After.
#      The HF cache makes each retry resume where it stopped (already-cached files
#      are not re-requested), so progress is preserved across retries and runs.
# ---------------------------------------------------------------------------
SNAPSHOT_WORKERS = 16
MAX_RETRIES      = 20
RETRY_BASE_SEC   = 10

os.environ['HF_HUB_ENABLE_HF_TRANSFER'] = '0'

# Manifest of remote paths already copied into OUTDIR. Re-runs skip them entirely,
# even when the HF cache is empty (e.g. on a fresh machine).
TRACK_FILE = '.downloaded_candidates.log'

repo_id, outdir, token = sys.argv[1], sys.argv[2], (sys.argv[3] or None)
manifest_dir = sys.argv[4]
range_args = sys.argv[5:]

# ---- manifest ----
def load_downloaded():
    track_path = os.path.join(manifest_dir, TRACK_FILE)
    if not os.path.exists(track_path):
        return set()
    with open(track_path, 'r', encoding='utf-8') as fhandle:
        return {line.strip() for line in fhandle if line.strip()}

def mark_downloaded(remote_path):
    track_path = os.path.join(manifest_dir, TRACK_FILE)
    os.makedirs(manifest_dir, exist_ok=True)
    with open(track_path, 'a', encoding='utf-8') as fhandle:
        fhandle.write(remote_path + '\n')

def _retry_after_secs(resp):
    if resp is None:
        return None
    ra = resp.headers.get('Retry-After')
    if ra:
        try:
            return float(ra)
        except ValueError:
            return None
    return None

def _is_rate_limited(exc):
    # 429 from an HfHubHTTPError
    if isinstance(exc, HfHubHTTPError):
        return exc.response is not None and exc.response.status_code == 429
    # snapshot_download wraps a failed HEAD/resolver as LocalEntryNotFoundError;
    # its underlying cause may be a 429. Treat any LocalEntryNotFoundError as
    # retryable since the HF cache makes the retry resume where it stopped.
    if isinstance(exc, LocalEntryNotFoundError):
        return True
    return False

def _retry_after(exc):
    resp = getattr(exc, 'response', None)
    if isinstance(exc, HfHubHTTPError):
        resp = exc.response
    else:
        # dig for the wrapped HTTPError with the response attached
        cause = exc
        while getattr(cause, '__cause__', None) is not None:
            cause = cause.__cause__
            if getattr(cause, 'response', None) is not None:
                resp = cause.response
                break
    return _retry_after_secs(resp)

def retry_snapshot(fn):
    attempt = 0
    while True:
        try:
            return fn()
        except (HfHubHTTPError, LocalEntryNotFoundError, HTTPError, URLError, OSError) as e:
            attempt += 1
            is_rate_limited = _is_rate_limited(e)
            secs = _retry_after(e)
            if secs is None:
                secs = (RETRY_BASE_SEC * (2 ** min(attempt, 5))) if is_rate_limited \
                    else RETRY_BASE_SEC * (2 + attempt)
            kind = 'rate-limit' if is_rate_limited else 'retry'
            print(f'  [{kind}] {e.__class__.__name__} (attempt {attempt}/{MAX_RETRIES}), sleeping {secs:.0f}s...')
            if attempt >= MAX_RETRIES:
                raise
            time.sleep(secs)

# ---- figure out which incidents to download (same range logic as before) ----
api = HfApi(token=token)
all_files = api.list_repo_files(repo_id, repo_type='dataset')

cand_re = re.compile(r'^candidates/incident_(\d+)/candidate_\d+/.+$')
available_ids = set()
for f in all_files:
    m = cand_re.match(f)
    if m:
        available_ids.add(m.group(1))
available_ids = sorted(available_ids, key=int)

num_available = len(available_ids)

if len(range_args) == 0:
    target_ids = available_ids
elif len(range_args) == 1:
    idx = int(range_args[0]) - 1
    if idx < 0 or idx >= num_available:
        print(f'Error: index {range_args[0]} is out of range (available: 1-{num_available})')
        sys.exit(1)
    target_ids = [available_ids[idx]]
else:
    start = int(range_args[0]) - 1
    end = int(range_args[1])
    if start < 0:
        print(f'Error: start index {range_args[0]} is out of range (available: 1-{num_available})')
        sys.exit(1)
    if end > num_available:
        print(f'Warning: end index {range_args[1]} exceeds available count ({num_available}), clamping.')
        end = num_available
    if start >= end:
        print(f'Error: start index {range_args[0]} must be less than end index {range_args[1]}')
        sys.exit(1)
    target_ids = available_ids[start:end]

# ---- download the WHOLE candidates tree (or selected incidents) at once ----
allow_patterns = []
for inc_id in target_ids:
    allow_patterns.append(f'candidates/incident_{inc_id}/*')
allow_patterns += ['candidates/candidate_status.csv', 'candidates/candidate_metadata.csv']

print(f'Found {num_available} incident(s) with candidates on HF; downloading folders for {len(target_ids)}.')
snap_dir = retry_snapshot(lambda: snapshot_download(
    repo_id=repo_id,
    repo_type='dataset',
    token=token,
    allow_patterns=allow_patterns,
    max_workers=SNAPSHOT_WORKERS,
))
print(f'snapshot downloaded to: {snap_dir}')

# ---- copy the snapshot's candidates/ tree into OUTDIR, stripping the prefix ----
already_downloaded = load_downloaded()
copied = 0
skipped = 0
for root, _, files in os.walk(snap_dir):
    for fn in files:
        rel = os.path.relpath(os.path.join(root, fn), snap_dir)
        if not rel.startswith('candidates/'):
            continue
        if rel in already_downloaded:
            skipped += 1
            continue
        dest = os.path.join(outdir, rel[len('candidates/'):])
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy(os.path.join(root, fn), dest)
        mark_downloaded(rel)
        copied += 1

print(f'Copied {copied} file(s) into {outdir} (skipped {skipped} already present).')
print('All done.')
EOF