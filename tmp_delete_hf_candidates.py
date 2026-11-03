"""TEMPORARY: delete detected candidates on Hugging Face for a CSV row range so
candidate_detection.ipynb regenerates them (it skips incidents that already have
candidates/incident_{ID}/ files).

Also removes those incidents' rows from candidates/candidate_status.csv and
candidates/candidate_metadata.csv, in the same commit as the first batch of deletions.

Dry run by default. Usage:
    python tmp_delete_hf_candidates.py            # preview
    python tmp_delete_hf_candidates.py --delete   # delete chips + clean summary CSVs

Auth: set HF_TOKEN (a token with write access to the dataset) or run `huggingface-cli login`.
"""
import argparse
import os
import re

import pandas as pd
from huggingface_hub import CommitOperationAdd, CommitOperationDelete, HfApi, hf_hub_download

CSV = 'landslides_from_2018_to_2026.csv'
START_IDX, END_IDX = 800, 1500  # same iloc[START:END] semantics as the notebook (end exclusive)
REPO_ID, REPO_TYPE, REVISION = 'sasudo2/landslides', 'dataset', 'main'
CAND_ROOT = 'candidates'
SUMMARY_CSVS = ('candidate_status.csv', 'candidate_metadata.csv')
BATCH = 500

ap = argparse.ArgumentParser()
ap.add_argument('--delete', action='store_true', help='actually delete (default: dry run)')
args = ap.parse_args()

ids = set(pd.read_csv(CSV).iloc[START_IDX:END_IDX]['id'].astype(int))
token = os.environ.get('HF_TOKEN')
api = HfApi(token=token)

targets = []
for path in api.list_repo_files(REPO_ID, repo_type=REPO_TYPE, revision=REVISION):
    m = re.match(rf'^{CAND_ROOT}/incident_(\d+)/', path)
    if m and int(m.group(1)) in ids:
        targets.append(path)

hit = {int(re.search(r'incident_(\d+)/', p).group(1)) for p in targets}
print(f'Rows {START_IDX}:{END_IDX} -> {len(ids)} incidents; {len(targets)} candidate file(s) in {len(hit)} incident folder(s) match')
for p in targets[:20]:
    print('  ', p)
if len(targets) > 20:
    print(f'   ... and {len(targets) - 20} more')

cleaned = {}  # filename -> (local path, rows removed)
for name in SUMMARY_CSVS:
    try:
        local = hf_hub_download(REPO_ID, f'{CAND_ROOT}/{name}', repo_type=REPO_TYPE, revision=REVISION, token=token)
    except Exception as e:
        print(f'{name}: could not download ({type(e).__name__}) - skipping')
        continue
    df = pd.read_csv(local)
    keep = ~df['incident_id'].astype(int).isin(ids)
    print(f'{name}: {int((~keep).sum())} of {len(df)} row(s) belong to these incidents')
    out = f'/tmp/{name}'
    df[keep].to_csv(out, index=False)
    cleaned[name] = (out, int((~keep).sum()))

if not args.delete:
    print('Dry run only. Re-run with --delete to remove these files and clean the summary CSVs.')
elif targets or any(n for _, n in cleaned.values()):
    csv_ops = [CommitOperationAdd(path_in_repo=f'{CAND_ROOT}/{n}', path_or_fileobj=p)
               for n, (p, removed) in cleaned.items() if removed]
    chunks = [targets[i:i + BATCH] for i in range(0, len(targets), BATCH)] or [[]]
    done = 0
    for k, chunk in enumerate(chunks):
        ops = [CommitOperationDelete(path_in_repo=p) for p in chunk]
        if k == 0:
            ops += csv_ops
        done += len(chunk)
        api.create_commit(
            repo_id=REPO_ID, repo_type=REPO_TYPE, revision=REVISION, operations=ops,
            commit_message=f'Remove candidates for incident rows {START_IDX}:{END_IDX} ({done}/{len(targets)})',
        )
        print(f'Deleted {done}/{len(targets)}')
