"""Small capability probes; never include credentials in persisted evidence."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


def probe_service(output_dir: Path = Path("data/tushare-probe")) -> dict:
    from finance_quant.data.providers import TushareSettings, TushareProvider

    provider = TushareProvider(TushareSettings.from_env())
    output_dir.mkdir(parents=True, exist_ok=True)
    requests = [
        ("daily", {"ts_code": "000001.SZ", "trade_date": "20260930"}),
        ("trade_cal", {"exchange": "SSE", "start_date": "20260901", "end_date": "20261015"}),
        ("adj_factor", {"ts_code": "000001.SZ", "trade_date": "20260930"}),
        ("daily_basic", {"ts_code": "000001.SZ", "trade_date": "20260930"}),
        ("stk_limit", {"ts_code": "000001.SZ", "trade_date": "20260930"}),
        ("index_daily", {"ts_code": "000300.SH", "start_date": "20260929", "end_date": "20260930"}),
        ("stock_basic", {"ts_code": "000001.SZ"}),
        ("fina_indicator", {"ts_code": "000001.SZ", "period": "20260630"}),
    ]
    evidence = []
    for name, params in requests:
        start = time.monotonic()
        try:
            frame = provider.query(name, **params)
            frame.to_parquet(output_dir / f"{name}.parquet", index=False)
            item = {"api": name, "status": "ok" if not frame.empty else "empty", "rows": len(frame), "columns": list(frame.columns)}
        except Exception as error:
            # Exception strings from remote providers are untrusted and may echo credentials.
            item = {"api": name, "status": "error", "error_type": type(error).__name__}
        item["elapsed_seconds"] = round(time.monotonic() - start, 2)
        evidence.append(item)
        print(json.dumps(item, ensure_ascii=False), flush=True)
        time.sleep(0.4)
    document = {"checked_at": datetime.now(timezone.utc).isoformat(), "provider": "third_party_tushare_compatible", "evidence": evidence}
    (output_dir / "capabilities.json").write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    return document


if __name__ == "__main__":
    probe_service()
