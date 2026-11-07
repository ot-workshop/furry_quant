"""Download official historical SW L1 membership, serially with resume."""

import argparse
import json

from finance_quant.data.industries import download_industries

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument(
        "--levels",
        nargs="+",
        default=["L1", "L2", "L3"],
        choices=["L1", "L2", "L3"],
        help="申万行业级别，默认全部",
    )
    args = parser.parse_args()
    history = download_industries(
        refresh=args.refresh,
        levels=tuple(args.levels),
        progress=lambda row: print(json.dumps(row), flush=True),
    )
    print(json.dumps({k: history.manifest[k] for k in ["id", "rows", "instruments", "policy"]}, ensure_ascii=False))
