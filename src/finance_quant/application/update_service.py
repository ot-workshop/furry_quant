"""Reference publication and revision windows shared by the market and index downloads."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from finance_quant.data.symbols import exchange_of


def refresh_dates(expected, covered, count):
    """Only already stored dates are revisions; missing dates remain separate work."""
    return expected[-count:].intersection(covered) if count else pd.DatetimeIndex([])


def publish_references(directory, plan):
    from finance_quant.data.shared_market import market_store

    directory = Path(directory)
    frames = {
        name: pd.read_parquet(directory / "reference" / f"{name}.parquet")
        for name in ("calendar", "instruments", "memberships", "benchmark")
    }
    common = {"provider_id": plan["provider_id"], "quality": "single_source_passed"}

    def day(value):
        return None if pd.isna(value) or value == "" else pd.Timestamp(value).date()

    components = {
        "trade_calendar": [
            {**common, "exchange": "SSE", "trade_date": day(row.date), "is_open": bool(row.is_open)}
            for row in frames["calendar"].itertuples()
        ],
        "security": [
            {
                **common,
                "ts_code": row.ts_code,
                "name": row.name,
                "exchange": exchange_of(row.ts_code),
                "list_date": day(row.list_date),
                "delist_date": day(row.delist_date),
            }
            for row in frames["instruments"].itertuples()
        ],
        "index_member": [
            {
                **common,
                "index_code": "399300.SZ",
                "ts_code": row.code,
                "effective_from": day(row.effective_from),
                "effective_to": day(row.effective_until),
                "snapshot_date": day(row.snapshot_date),
                "weight": float(row.weight),
                "timing_basis": "monthly_snapshot_next_session_unverified",
            }
            for row in frames["memberships"].itertuples()
        ],
        "benchmark": [
            {
                **common,
                "ts_code": "000300.SH",
                "trade_date": day(row.date),
                "close": float(row.close),
                "pre_close": float(row.pre_close),
                "return_type": "price",
            }
            for row in frames["benchmark"].itertuples()
        ],
    }
    checks = [
        {"component": name, "partition_key": plan["id"], "status": "complete", "row_count": len(rows)}
        for name, rows in components.items()
        if rows
    ]
    return market_store().publish_references(components, batch_id="references-" + plan["id"], checks=checks)
