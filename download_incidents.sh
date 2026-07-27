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
# landslide_annotator expects):
#   $OUTDIR/incident_{ID}/candidate_{N}/incident_{ID}_candidate_{N}_{before,after,slope,mask}.tif
#   $OUTDIR/candidate_status.csv
#   $OUTDIR/candidate_metadata.csv
#
# Index range (1-based, inclusive):
#   200 300       -> download the 200th to 300th incidents in sorted order
# Usage:
#   ./download_incidents.sh                     # ALL incidents with candidates -> ./incidents
#   ./download_incidents.sh 200                 # 200th incident -> ./incidents
#   ./download_incidents.sh 200 300             # 200th to 300th -> ./incidents
#   ./download_incidents.sh 200 300 ./data      # 200th to 300th -> ./data
#   ./download_incidents.sh ./data              # all -> ./data

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

python3 - "$REPO" "$OUTDIR" "$TOKEN" "${RANGE_ARGS[@]+"${RANGE_ARGS[@]}"}" << 'EOF'
import os
import re
import shutil
import sys

from huggingface_hub import HfApi, hf_hub_download

repo_id, outdir, token = sys.argv[1], sys.argv[2], (sys.argv[3] or None)
range_args = sys.argv[4:]

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

print(f'Found {num_available} incident(s) with candidates on HF; downloading {len(target_ids)}.')

for inc_id in target_ids:
    prefix = f'candidates/incident_{inc_id}/'
    files = [f for f in all_files if f.startswith(prefix)]
    print(f'  incident_{inc_id}: {len(files)} file(s)')
    for f in files:
        local_path = hf_hub_download(repo_id=repo_id, repo_type='dataset', filename=f, token=token)
        rel = f[len('candidates/'):]  # incident_{ID}/candidate_{N}/incident_{ID}_candidate_{N}_*.tif
        dest = os.path.join(outdir, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy(local_path, dest)

for csv_name in ('candidate_status.csv', 'candidate_metadata.csv'):
    remote = f'candidates/{csv_name}'
    if remote in all_files:
        local_path = hf_hub_download(repo_id=repo_id, repo_type='dataset', filename=remote, token=token)
        shutil.copy(local_path, os.path.join(outdir, csv_name))
        print(f'  Downloaded {csv_name}')

print('All done.')
EOF