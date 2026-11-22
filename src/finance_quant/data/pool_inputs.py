"""Shared eligibility inputs for index and user-selected stock pools."""

from pathlib import Path

import pandas as pd

from finance_quant.checksums import sha256_bytes
from finance_quant.data.market import load_market
from finance_quant.data.symbols import to_qlib
from finance_quant.research.universe import select_universe

NAME_COLUMNS = ["instrument", "name", "start_date", "end_date", "ann_date"]


def security_records(root, manifest):
    """Read only the security history pinned to this dataset, never today's master."""
    root = Path(root)
    saved = manifest.get("security_history", {})
    instruments = pd.DataFrame(saved.get("instruments", []), columns=["ts_code", "list_date", "delist_date"])
    names = pd.DataFrame(saved.get("names", []), columns=NAME_COLUMNS)
    if "instruments.parquet" in manifest.get("reference_files", {}):
        instruments = pd.read_parquet(root / "reference/instruments.parquet")
    if "name_history.parquet" in manifest.get("reference_files", {}):
        names = pd.read_parquet(root / "reference/name_history.parquet")
    instruments = instruments.reindex(columns=["ts_code", "list_date", "delist_date"])
    for column in ["start_date", "end_date", "ann_date"]:
        names[column] = pd.to_datetime(names[column])
    return instruments, names


def security_coverage(root, manifest, codes):
    instruments, names = security_records(root, manifest)
    symbols = {code[-2:] + code[:6] for code in codes}
    listed = pd.to_datetime(instruments.list_date, format="%Y%m%d", errors="coerce")
    listing = set(instruments.loc[listed.notna(), "ts_code"]) & set(codes)
    named = set(names.instrument)
    return {
        "st_coverage": {"covered": len(set(names.instrument) & symbols), "total": len(codes)},
        "listing_coverage": {"covered": len(listing), "total": len(codes)},
        "security_by_code": {
            code: {"st_coverage": code[-2:] + code[:6] in named, "listing_coverage": code in listing} for code in codes
        },
    }


def filter_input(inputs, root, manifest, filters, start, end, *, industry=None, kind="index", codes=None):
    market, benchmark, sessions, meta = inputs
    instruments, names = security_records(root, manifest)
    if "delist_date" not in market:
        market["delist_date"] = pd.NaT
    if "list_date" not in market:
        market["list_date"] = pd.NaT
    master = instruments.set_index("ts_code")
    for code, row in master.iterrows():
        mask = market.instrument.eq(code[-2:] + code[:6])
        for column in ("list_date", "delist_date"):
            market.loc[mask, column] = pd.to_datetime(row[column] or None, format="%Y%m%d")
    if codes is not None:
        market = market.loc[market.instrument.isin({code[-2:] + code[:6] for code in codes})].copy()
    market, selected = select_universe(
        market,
        sessions,
        start,
        end,
        filters,
        names,
        industry=industry,
        base_membership=meta.get("membership_by_execution_date") if kind == "index" else None,
    )
    return (
        market,
        benchmark,
        sessions,
        {
            **meta,
            **selected,
            "universe_kind": kind,
            "exit_reason": "pool_exit",
            "universe_label": "沪深300历史成分股条件股票池" if kind == "index" else "自选股票条件股票池",
        },
    )


def load_snapshot_input(root, start, end, lookback, *, filters, industry=None, codes=None, frozen_snapshot=None):
    if frozen_snapshot is None:
        market, benchmark, calendar, manifest = load_market(Path(root))
        manifest_sha256 = sha256_bytes((Path(root) / "manifest.json").read_bytes())
    else:
        if Path(root) != frozen_snapshot.root:
            raise ValueError("固定池预检路径与冻结快照不一致")
        market, benchmark, calendar, manifest = frozen_snapshot.inputs
        market = market.copy()  # Candidate-specific filters must not mutate the shared verified input.
        manifest_sha256 = frozen_snapshot.manifest_sha256
    if "is_suspended" not in market:
        # Legacy snapshots have no status flag. Zero-volume rows are excluded
        # conservatively; they are not evidence of a confirmed suspension.
        market["is_suspended"] = market.volume.le(0)
    if codes is not None:
        if not set(codes).issubset(manifest["codes"]):
            raise ValueError("所选股票不在快照中")
        market = market.loc[market.instrument.isin({to_qlib(code) for code in codes})].copy()
        manifest = {**manifest, "codes": sorted(codes)}
    market["eligible_history"] = market.groupby("instrument").cumcount().ge(lookback)
    sessions = pd.DatetimeIndex(calendar.loc[calendar.is_open, "date"])
    required = max(lookback, filters.amount_window - 1 if filters.min_avg_amount is not None else 0) + 1
    position = sessions.get_loc(pd.Timestamp(start))
    if position < required or sessions[position - required] < market.date.min():
        raise ValueError("股票池筛选和因子计算的历史预热不足，请延后回测起点")
    market = market.loc[market.date.between(sessions[position - required], pd.Timestamp(end))].copy()
    meta = {
        **manifest,
        "manifest_sha256": manifest_sha256,
        "initial_date": sessions[position - 1].strftime("%Y-%m-%d"),
        "membership_policy": "user-selected candidates; signal-date filters; next-session execution",
        "validation_counts": manifest.get("validation_counts", {}),
    }
    return filter_input(
        (market, benchmark, sessions, meta), root, manifest, filters, start, end, industry=industry, kind="custom"
    )
