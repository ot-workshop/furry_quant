"""Download official historical SW L1 membership, serially with resume."""
import json
import argparse
from finance_quant.data.industries import download_industries

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--refresh',action='store_true')
    history=download_industries(refresh=parser.parse_args().refresh,progress=lambda row: print(json.dumps(row),flush=True))
    print(json.dumps({k:history.manifest[k] for k in ['id','rows','instruments','policy']},ensure_ascii=False))
