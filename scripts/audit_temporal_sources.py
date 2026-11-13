"""Bounded official-source probes and a read-only audit of the saved warehouse.

No credentials, provider response messages or portfolio changes are emitted.
Raw public data is retained in a separate, timestamped audit directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from finance_quant.runtime import configure_runtime

configure_runtime()

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from finance_quant.application.market_download import warehouse_roots
from finance_quant.data.industries import load_industries
from finance_quant.data.providers.tushare import TushareProvider, TushareProviderError, TushareSettings
from finance_quant.paths import data_root


def main():
    pa.set_cpu_count(1)
    pa.set_io_thread_count(1)
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--warehouse", type=Path, help="沪深300历史成分视图目录；默认取最新发布的共享下载版本")
    args = parser.parse_args()

    views = [args.warehouse] if args.warehouse else warehouse_roots(data_root())
    if not views:
        parser.error("没有已发布的沪深300历史成分视图，请先在数据管理中完成沪深300行情下载")
    warehouse = views[0]
    root = data_root() / "audits" / ("temporal-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f"))
    root.mkdir(parents=True)
    manifest = json.loads((warehouse / "manifest.json").read_text(encoding="utf-8"))
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "warehouse_sha256": hashlib.sha256((warehouse / "manifest.json").read_bytes()).hexdigest(),
        "assets": len(manifest["assets"]),
        "source_probes": [],
        "point_in_time_certified": False,
    }
    stock_days = 0
    future_dates = 0
    date_fields = 0
    carried = 0
    for code, asset in manifest["assets"].items():
        path = warehouse / asset["file"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == asset["sha256"]
        columns = ["date", "valuation_carried"]
        if "valuation_asof" in pq.read_schema(path).names:
            columns += ["valuation_asof"]
        frame = pd.read_parquet(path, columns=columns, use_threads=False)
        stock_days += len(frame)
        carried += int(frame.valuation_carried.sum())
        if "valuation_asof" in frame:
            date_fields += 1
            future_dates += int(pd.to_datetime(frame.valuation_asof, errors="coerce").gt(frame.date).sum())
        del frame
    report["valuation_storage"] = {
        "stock_days": stock_days,
        "assets_with_valuation_asof": date_fields,
        "carried_pe_rows": carried,
        "future_observation_dates": future_dates,
        "publication_time_and_financial_vintages": "not_present_in_daily_basic",
    }
    intervals = pd.read_parquet(warehouse / "reference/memberships.parquet")
    report["membership"] = {
        "snapshots": int(intervals.snapshot_date.nunique()),
        "policy": manifest["membership_policy"],
        "announcement_timestamp": "not_in_index_weight",
        "examples": intervals.loc[
            intervals.snapshot_date.between("2023-11-01", "2023-12-31"), ["snapshot_date", "effective_from"]
        ]
        .drop_duplicates()
        .astype(str)
        .to_dict("records"),
    }
    industry = load_industries()
    f = industry.frame
    report["industry"] = {
        "id": industry.manifest["id"],
        "rows": len(f),
        "instruments": int(f.instrument.nunique()),
        "new_taxonomy_backcast_before_transition": int(
            (f.classification.eq("SW2021") & f.in_date.lt("2021-12-13")).sum()
        ),
        "policy": industry.manifest["policy"],
        "announcement_time": "not_supplied",
    }
    if not args.offline:
        provider = TushareProvider(TushareSettings(TushareSettings.from_env().token))
        queries = [
            (
                "daily_basic",
                {
                    "ts_code": code,
                    "start_date": "20230420",
                    "end_date": "20230505",
                    "fields": "ts_code,trade_date,close,pe_ttm,pb,total_mv",
                },
            )
            for code in ["600036.SH", "600519.SH"]
        ]
        queries += [
            (
                "income",
                {
                    "ts_code": "600036.SH",
                    "start_date": "20230101",
                    "end_date": "20231231",
                    "fields": "ts_code,ann_date,f_ann_date,end_date,report_type,update_flag,n_income_attr_p",
                },
            ),
            (
                "balancesheet",
                {
                    "ts_code": "600036.SH",
                    "start_date": "20230101",
                    "end_date": "20231231",
                    "fields": "ts_code,ann_date,f_ann_date,end_date,report_type,update_flag,total_hldr_eqy_exc_min_int,oth_eqt_tools",
                },
            ),
            ("index_weight", {"index_code": "399300.SZ", "start_date": "20231201", "end_date": "20231231"}),
        ]
        for i, (api, params) in enumerate(queries):
            try:
                frame = provider.query(api, **params)
                path = root / f"{i}-{api}.parquet"
                frame.to_parquet(path, index=False)
                item = {
                    "api": api,
                    "params": params,
                    "rows": len(frame),
                    "columns": list(frame),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "status": "received",
                }
                if api == "daily_basic" and len(frame):
                    cached = pd.read_parquet(warehouse / manifest["assets"][params["ts_code"]]["file"])
                    observed = frame.assign(date=pd.to_datetime(frame.trade_date.astype(str)))
                    joined = cached.merge(
                        observed[["date", "pe_ttm", "pb"]], on="date", suffixes=("_archived", "_fresh")
                    )
                    item["archive_comparison"] = {
                        "rows": len(joined),
                        "max_abs_pe_difference": float((joined.pe_ttm_archived - joined.pe_ttm_fresh).abs().max()),
                        "max_abs_pb_difference": float((joined.pb_archived - joined.pb_fresh).abs().max()),
                        "note": "Only compares two modern retrievals; cannot prove what was published in 2023.",
                    }
                if api == "index_weight" and len(frame):
                    item["observation_dates"] = sorted(frame.trade_date.astype(str).unique().tolist())
                report["source_probes"].append(item)
                print(json.dumps(item, ensure_ascii=False), flush=True)
            except TushareProviderError as error:
                item = {"api": api, "status": "unavailable", "category": error.category}
                report["source_probes"].append(item)
                print(json.dumps(item), flush=True)
    path = root / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "report": str(path),
                "assets": report["assets"],
                "stock_days": stock_days,
                "point_in_time_certified": False,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
