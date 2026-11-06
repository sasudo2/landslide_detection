"""TEMPORARY: delete raw incident imagery on Hugging Face for a CSV row range so
incident_download.ipynb re-downloads it (e.g. with the new dry-season selection).

Dry run by default. Usage:
    python tmp_delete_hf_incidents.py                 # preview optical files to delete
    python tmp_delete_hf_incidents.py --delete        # actually delete them
    python tmp_delete_hf_incidents.py --all --delete  # delete every file in those incident folders

Auth: set HF_TOKEN or run `huggingface-cli login`.
"""
import argparse
import os
import re
from pathlib import Path

import pandas as pd
from huggingface_hub import CommitOperationDelete, HfApi

CSV = Path(__file__).resolve().parents[2] / 'data/reference/landslides_from_2018_to_2026.csv'
START_IDX, END_IDX = 800, 1450  # same iloc[START:END] semantics as the notebook (end exclusive)
REPO_ID, REPO_TYPE, REVISION = 'sasudo2/landslides', 'dataset', 'main'
OPTICAL_SUFFIXES = ('_after.tif', '_planet_before.tif', '_gee_before.tif')
BATCH = 500

ap = argparse.ArgumentParser()
ap.add_argument('--delete', action='store_true', help='actually delete (default: dry run)')
ap.add_argument('--all', action='store_true', help='delete all files in the incident folders, not just optical imagery')
args = ap.parse_args()

ids = set(pd.read_csv(CSV).iloc[START_IDX:END_IDX]['id'].astype(int))
api = HfApi(token=os.environ.get('HF_TOKEN'))

targets = []
for path in api.list_repo_files(REPO_ID, repo_type=REPO_TYPE, revision=REVISION):
    m = re.match(r'^raw_images/raw_incidents/incident_(\d+)/(.+)$', path)
    if m and int(m.group(1)) in ids and (args.all or m.group(2).endswith(OPTICAL_SUFFIXES)):
        targets.append(path)

hit = {int(re.search(r'incident_(\d+)/', p).group(1)) for p in targets}
print(f'Rows {START_IDX}:{END_IDX} -> {len(ids)} incidents; {len(targets)} file(s) in {len(hit)} incident folder(s) match')
for p in targets[:20]:
    print('  ', p)
if len(targets) > 20:
    print(f'   ... and {len(targets) - 20} more')

if not args.delete:
    print('Dry run only. Re-run with --delete to remove these files.')
elif targets:
    for i in range(0, len(targets), BATCH):
        chunk = targets[i:i + BATCH]
        api.create_commit(
            repo_id=REPO_ID, repo_type=REPO_TYPE, revision=REVISION,
            operations=[CommitOperationDelete(path_in_repo=p) for p in chunk],
            commit_message=f'Remove raw imagery for incidents rows {START_IDX}:{END_IDX} ({i + len(chunk)}/{len(targets)})',
        )
        print(f'Deleted {i + len(chunk)}/{len(targets)}')
