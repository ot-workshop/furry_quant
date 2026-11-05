"""Probe which Tushare APIs the configured token can use.

Usage: ``uv run python scripts/probe_tushare.py [--output DIR] [--date YYYYMMDD]``.
Credentials come from ``TUSHARE_TOKEN``; they are never written to the evidence files.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


def probe_service(output_dir: Path, trade_date: str) -> dict:
    from finance_quant.data.providers import TushareProvider, TushareSettings

    provider = TushareProvider(TushareSettings.from_env())
    output_dir.mkdir(parents=True, exist_ok=True)
    day = pd.Timestamp(trade_date)
    month_start = day.replace(day=1).strftime("%Y%m%d")
    quarter_end = (day - pd.offsets.QuarterEnd(1)).strftime("%Y%m%d")
    requests = [
        ("daily", {"ts_code": "000001.SZ", "trade_date": trade_date}),
        ("trade_cal", {"exchange": "SSE", "start_date": month_start, "end_date": trade_date}),
        ("adj_factor", {"ts_code": "000001.SZ", "trade_date": trade_date}),
        ("daily_basic", {"ts_code": "000001.SZ", "trade_date": trade_date}),
        ("stk_limit", {"ts_code": "000001.SZ", "trade_date": trade_date}),
        ("index_daily", {"ts_code": "000300.SH", "start_date": month_start, "end_date": trade_date}),
        ("stock_basic", {"ts_code": "000001.SZ"}),
        ("fina_indicator", {"ts_code": "000001.SZ", "period": quarter_end}),
    ]
    evidence = []
    for name, params in requests:
        start = time.monotonic()
        try:
            frame = provider.query(name, **params)
            frame.to_parquet(output_dir / f"{name}.parquet", index=False)
            item = {
                "api": name,
                "status": "ok" if not frame.empty else "empty",
                "rows": len(frame),
                "columns": list(frame.columns),
            }
        except Exception as error:
            # Exception strings from remote providers are untrusted and may echo credentials.
            item = {"api": name, "status": "error", "error_type": type(error).__name__}
        item["elapsed_seconds"] = round(time.monotonic() - start, 2)
        evidence.append(item)
        print(json.dumps(item, ensure_ascii=False), flush=True)
        time.sleep(0.4)
    document = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "provider": "third_party_tushare_compatible",
        "evidence": evidence,
    }
    (output_dir / "capabilities.json").write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    return document


def main() -> None:
    import argparse

    from finance_quant.runtime import configure_runtime

    configure_runtime()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=Path("storage/tushare-probe"))
    yesterday = (pd.Timestamp.now(tz="Asia/Shanghai").normalize() - pd.Timedelta(days=1)).strftime("%Y%m%d")
    parser.add_argument("--date", default=yesterday, help="Trade date to query, YYYYMMDD")
    args = parser.parse_args()
    probe_service(args.output, args.date)


if __name__ == "__main__":
    main()
