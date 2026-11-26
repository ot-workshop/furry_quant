"""Pinned offline research input from the historical index warehouse."""

from __future__ import annotations

import json
from itertools import pairwise
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from finance_quant.checksums import file_sha256, sha256_bytes
from finance_quant.data.index_universe import historical_codes, members_on
from finance_quant.data.pool_inputs import security_coverage
from finance_quant.data.symbols import to_qlib
from finance_quant.data.temporal_audit import restore_valuation_dates


def warehouse_header(root, *, expected_sha=None):
    root = Path(root).resolve()
    path = root / "manifest.json"
    raw = path.read_bytes()
    digest = sha256_bytes(raw)
    if expected_sha is not None and digest != expected_sha:
        raise ValueError("排队期间历史仓库版本已变化，请重新提交")
    manifest = json.loads(raw)
    if manifest.get("schema_version") != "historical-index-warehouse-v1" or not manifest.get("assets"):
        raise ValueError("历史成分数据仓尚未完整发布")
    calendar_path = root / "reference/calendar.parquet"
    if file_sha256(calendar_path) != manifest["reference_files"]["calendar.parquet"]:
        raise ValueError("历史仓库交易日历校验失败")
    calendar = pd.read_parquet(calendar_path)
    return manifest, calendar, digest


def warehouse_option(root, *, expected_sha=None):
    try:
        manifest, calendar, digest = warehouse_header(root, expected_sha=expected_sha)
    except (OSError, ValueError, KeyError):
        return None
    dates = calendar.loc[
        calendar.is_open & calendar.date.between(pd.Timestamp(manifest["data_start"]), pd.Timestamp(manifest["end"])),
        "date",
    ]
    return {
        "id": "csi300-" + digest[:20],
        "manifest_sha256": digest,
        "label": "沪深300历史成分股（每日动态股票池）",
        **({"shared_version": manifest["shared_version"]} if manifest.get("shared_version") else {}),
        "start": manifest["start"],
        "end": manifest["end"],
        "data_start": manifest["data_start"],
        "trading_dates": dates.dt.strftime("%Y-%m-%d").tolist(),
        "codes_count": len(manifest["assets"]),
        "codes": sorted(manifest["assets"]),
        **security_coverage(root, manifest, sorted(manifest["assets"])),
        "membership_policy": manifest["membership_policy"],
        "point_in_time_certified": False,
        "note": "月度快照从下一交易日起使用，可能滞后真实调仓；不是精确公告生效日认证",
    }


def load_historical_input(root, start, end, lookback, *, expected_sha=None):
    root = Path(root).resolve()
    manifest, calendar, digest = warehouse_header(root, expected_sha=expected_sha)
    sessions = pd.DatetimeIndex(calendar.loc[calendar.is_open, "date"])
    begin, finish = pd.Timestamp(start), pd.Timestamp(end)
    if (
        begin not in sessions
        or finish not in sessions
        or not pd.Timestamp(manifest["start"]) <= begin <= finish <= pd.Timestamp(manifest["end"])
    ):
        raise ValueError("回测区间须位于历史仓库研究范围，且起止为实际交易日")
    position = sessions.get_loc(begin)
    required = lookback + 1
    if position < required:
        raise ValueError("历史仓库预热不足")
    left = sessions[position - required]
    signal_start = sessions[position - 1]
    if left < pd.Timestamp(manifest["data_start"]):
        raise ValueError(f"因子需要{required}个回测前交易日，请延后回测起点或建立更长预热数据仓")
    for name, sha in manifest["reference_files"].items():
        if file_sha256(root / "reference" / name) != sha:
            raise ValueError("历史参考数据校验失败")
    intervals = pd.read_parquet(root / "reference/memberships.parquet")
    instruments = pd.read_parquet(root / "reference/instruments.parquet").set_index("ts_code")
    codes = historical_codes(intervals, begin, finish)
    frames = []
    for code in codes:
        asset = manifest["assets"][code]
        path = (root / asset["file"]).resolve()
        if not path.is_relative_to(root) or file_sha256(path) != asset["sha256"]:
            raise ValueError(f"{code}历史行情校验失败")
        columns = [
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
        ]
        optional = [
            "valuation_carried",
            "valuation_asof",
            "pe_ttm_asof",
            "pb_asof",
            "total_mv_asof",
            "valuation_available_at",
            "pe_ttm_available_at",
            "pb_available_at",
            "total_mv_available_at",
        ]
        existing = set(pq.read_schema(path).names)
        frame = pd.read_parquet(path, columns=[c for c in columns + optional if c in existing])
        if "history_sessions_before" not in frame:
            frame["history_sessions_before"] = np.arange(len(frame))
        expected = sessions[(sessions >= pd.Timestamp(asset["start"])) & (sessions <= pd.Timestamp(asset["end"]))]
        if not pd.DatetimeIndex(frame.date).equals(expected):
            raise ValueError(f"{code}生命周期内存在行情缺口")
        frame = restore_valuation_dates(frame)
        frame = frame.loc[frame.date.between(left, finish)].copy()
        if frame.empty:
            continue
        frame["eligible_history"] = frame.history_sessions_before.ge(lookback)
        frame["selection_member"] = False
        frame["list_date"] = pd.to_datetime(str(instruments.loc[code, "list_date"]), format="%Y%m%d")
        frames.append(frame)
    market = pd.concat(frames, ignore_index=True).sort_values(["instrument", "date"]).reset_index(drop=True)
    membership = {}
    declared = {}
    exclusions = {}
    for day, next_day in pairwise(sessions):
        if day < signal_start or next_day > finish:
            continue
        members = members_on(intervals, next_day)
        # Effective_from is the session AFTER snapshot_date. Thus every selected
        # snapshot is at most signal day, under the explicitly stated assumption.
        active = intervals.loc[
            intervals.code.isin(members)
            & intervals.effective_from.le(next_day)
            & (intervals.effective_until.isna() | intervals.effective_until.gt(next_day))
        ]
        if active.snapshot_date.gt(day).any():
            raise ValueError("成分名单包含信号日之后的信息")
        ids = {to_qlib(code) for code in members}
        membership[next_day.strftime("%Y-%m-%d")] = sorted(ids)
        declared[day.strftime("%Y-%m-%d")] = len(ids)
        rows = market.date.eq(day) & market.instrument.isin(ids)
        market.loc[rows, "selection_member"] = True
        missing = ids - set(market.loc[rows, "instrument"])
        exclusions[day.strftime("%Y-%m-%d")] = sorted(missing)
    benchmark = pd.read_parquet(root / "reference/benchmark.parquet")
    benchmark = benchmark.loc[benchmark.date.between(left, finish)].copy()
    meta = {
        "id": "csi300-" + digest[:20],
        "source": "official_tushare_historical_warehouse",
        "snapshot_dir": str(root),
        "manifest_sha256": digest,
        "start": left.strftime("%Y%m%d"),
        "end": finish.strftime("%Y%m%d"),
        "benchmark": "000300.SH",
        "codes": list(codes),
        "membership_policy": manifest["membership_policy"],
        "point_in_time_certified": False,
        "initial_date": signal_start.strftime("%Y-%m-%d"),
        "membership_by_execution_date": membership,
        "declared_universe": declared,
        "lifecycle_exclusions": exclusions,
        "validation_counts": {
            str(k): int(v)
            for k, v in pd.Series([manifest["assets"][c]["validation_level"] for c in codes]).value_counts().items()
        },
    }
    return market, benchmark, sessions, meta


def execution_market(market, sessions, end):
    """Only account valuation can carry beyond lifecycle; never a trade/feature."""
    frames = []
    for instrument, part in market.groupby("instrument", sort=False):
        part = part.sort_values("date").copy()
        part["execution_available"] = ~part.is_suspended
        part["execution_state"] = np.where(part.is_suspended, "suspended", "active")
        last = part.iloc[-1]
        tail = sessions[(sessions > last.date) & (sessions <= pd.Timestamp(end))]
        if len(tail):
            carried = pd.DataFrame([last.to_dict() for _ in tail])
            carried["date"] = tail
            for field in ["open", "high", "low", "close", "pre_close"]:
                carried[field] = float(last.close)
            carried["volume"] = 0.0
            carried["amount"] = 0.0
            carried["change"] = 0.0
            carried["execution_available"] = False
            carried["execution_state"] = "outside_listing_lifecycle"
            carried["is_suspended"] = True  # Qlib valuation-only row, state retains lifecycle distinction.
            part = pd.concat([part, carried], ignore_index=True)
        frames.append(part)
    return pd.concat(frames, ignore_index=True)
