"""Immutable all-market research views over the shared quotation warehouse."""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from finance_quant.checksums import file_sha256, sha256_bytes
from finance_quant.data.pool_inputs import security_coverage
from finance_quant.data.security_history import ST_POLICY
from finance_quant.data.temporal_audit import restore_valuation_dates
from finance_quant.research.universe import select_universe

SCHEMA = "historical-market-warehouse-v1"


def market_roots(data_root):
    roots = [
        path.parent
        for path in (Path(data_root) / "shared-market/versions").glob("*/market-view/manifest.json")
        if (path.parent.parent / "version.json").exists()
    ]
    return sorted(
        (path.resolve() for path in roots),
        key=lambda path: (path.parent / "version.json").stat().st_mtime,
        reverse=True,
    )


def market_header(root, *, expected_sha=None):
    root = Path(root)
    raw = (root / "manifest.json").read_bytes()
    digest = sha256_bytes(raw)
    if expected_sha is not None and digest != expected_sha:
        raise ValueError("排队期间股票池行情版本已变化，请重新提交")
    manifest = json.loads(raw)
    if manifest.get("schema_version") != SCHEMA or not manifest.get("assets"):
        raise ValueError("股票池行情版本尚未完整发布")
    calendar_path = root / "reference/calendar.parquet"
    if file_sha256(calendar_path) != manifest["reference_files"]["calendar.parquet"]:
        raise ValueError("股票池交易日历校验失败")
    return manifest, pd.read_parquet(calendar_path), digest


def market_option(root, *, expected_sha=None):
    try:
        manifest, calendar, digest = market_header(root, expected_sha=expected_sha)
    except (OSError, ValueError, KeyError):
        return None
    complete = manifest.get("scope") == "all_market" and not manifest.get("restricted")
    dates = calendar.loc[
        calendar.is_open & calendar.date.between(pd.Timestamp(manifest["data_start"]), pd.Timestamp(manifest["end"])),
        "date",
    ]
    return {
        "id": "market-" + digest[:20],
        "manifest_sha256": digest,
        "label": "沪深A股全市场" if complete else f"已下载股票集合（{len(manifest['assets'])}只）",
        "full_market": complete,
        "shared_version": manifest["shared_version"],
        "start": manifest["start"],
        "end": manifest["end"],
        "data_start": manifest["data_start"],
        "trading_dates": dates.dt.strftime("%Y-%m-%d").tolist(),
        "codes_count": len(manifest["assets"]),
        "codes": sorted(manifest["assets"]),
        "membership_policy": manifest["membership_policy"],
        "point_in_time_certified": False,
        "st_policy": ST_POLICY,
        **security_coverage(root, manifest, list(manifest["assets"])),
        "note": "按历史上市生命周期与信号日条件筛选；下载子集不代表完整全市场。",
    }


def load_market_input(root, start, end, lookback, *, filters, industry=None, expected_sha=None, codes=None):
    root = Path(root).resolve()
    manifest, calendar, digest = market_header(root, expected_sha=expected_sha)
    sessions = pd.DatetimeIndex(calendar.loc[calendar.is_open, "date"])
    begin, finish = pd.Timestamp(start), pd.Timestamp(end)
    if (
        begin not in sessions
        or finish not in sessions
        or not pd.Timestamp(manifest["start"]) <= begin <= finish <= pd.Timestamp(manifest["end"])
    ):
        raise ValueError("回测区间须在行情版本内，且起止为交易日")
    required = max(lookback, filters.amount_window - 1 if filters.min_avg_amount is not None else 0) + 1
    position = sessions.get_loc(begin)
    if position < required or sessions[position - required] < pd.Timestamp(manifest["data_start"]):
        raise ValueError(f"股票池与因子需要{required}个回测前交易日，请补充预热数据或延后回测起点")
    left, signal_start = sessions[position - required], sessions[position - 1]
    for name, expected in manifest["reference_files"].items():
        if file_sha256(root / "reference" / name) != expected:
            raise ValueError("股票池参考数据校验失败")
    instruments = pd.read_parquet(root / "reference/instruments.parquet").set_index("ts_code")
    frames = []
    columns = {
        "instrument",
        "date",
        "name",
        "industry",
        "open",
        "high",
        "low",
        "close",
        "pre_close",
        "volume",
        "amount",
        "change",
        "adj_factor",
        "pe_ttm",
        "pb",
        "total_mv",
        "up_limit",
        "down_limit",
        "is_suspended",
        "history_sessions_before",
        "reviewed_unbounded_session",
        "validation_level",
        "valuation_carried",
        "valuation_asof",
        "valuation_available_at",
        *[f"{field}_{suffix}" for field in ("pe_ttm", "pb", "total_mv") for suffix in ("asof", "available_at")],
    }
    for code, asset in manifest["assets"].items():
        if codes is not None and code not in codes:
            continue
        if code.endswith(".SH") and not code.startswith(("60", "688")):
            continue  # CDR archives have no implemented A-share execution rules.
        path = (root / asset["file"]).resolve()
        if not path.is_relative_to(root) or file_sha256(path) != asset["sha256"]:
            raise ValueError(f"{code}股票池行情校验失败")
        existing = set(pq.read_schema(path).names)
        part = pd.read_parquet(path, columns=sorted(columns & existing))
        expected = sessions[(sessions >= pd.Timestamp(asset["start"])) & (sessions <= pd.Timestamp(asset["end"]))]
        if not pd.DatetimeIndex(part.date).equals(expected):
            raise ValueError(f"{code}生命周期内存在行情缺口")
        if "history_sessions_before" not in part:
            part["history_sessions_before"] = np.arange(len(part))
        part = restore_valuation_dates(part)
        part = part.loc[part.date.between(left, finish)].copy()
        if part.empty:
            continue
        master = instruments.loc[code]
        part["list_date"] = pd.to_datetime(master.list_date, format="%Y%m%d")
        part["delist_date"] = (
            pd.to_datetime(master.delist_date or None, format="%Y%m%d") if pd.notna(master.delist_date) else pd.NaT
        )
        part["eligible_history"] = part.history_sessions_before.ge(lookback)
        frames.append(part)
    if not frames:
        raise ValueError("回测范围内没有可用行情")
    market = pd.concat(frames, ignore_index=True).sort_values(["instrument", "date"]).reset_index(drop=True)
    names_path = root / "reference/name_history.parquet"
    names = (
        pd.read_parquet(names_path)
        if "name_history.parquet" in manifest["reference_files"]
        else pd.DataFrame(columns=["instrument", "name", "start_date", "end_date", "ann_date"])
    )
    market, pool = select_universe(market, sessions, start, end, filters, names, industry=industry)
    benchmark = pd.read_parquet(root / "reference/benchmark.parquet")
    benchmark = benchmark.loc[benchmark.date.between(left, finish)].copy()
    counts = pd.Series(
        [item["validation_level"] for code, item in manifest["assets"].items() if codes is None or code in codes]
    ).value_counts()
    return (
        market,
        benchmark,
        sessions,
        {
            "id": "market-" + digest[:20],
            "source": "official_tushare_market_warehouse",
            "snapshot_dir": str(root),
            "manifest_sha256": digest,
            "start": left.strftime("%Y%m%d"),
            "end": finish.strftime("%Y%m%d"),
            "benchmark": "000300.SH",
            "codes": sorted(codes if codes is not None else manifest["assets"]),
            "universe_kind": "custom" if codes is not None else "market",
            "exit_reason": "pool_exit",
            "universe_label": (
                "自选股票条件股票池"
                if codes is not None
                else "沪深A股全市场动态股票池"
                if manifest["scope"] == "all_market" and not manifest["restricted"]
                else "已下载集合动态股票池"
            ),
            "membership_policy": manifest["membership_policy"],
            "point_in_time_certified": False,
            "initial_date": signal_start.strftime("%Y-%m-%d"),
            "validation_counts": {str(key): int(value) for key, value in counts.items()},
            **pool,
        },
    )
