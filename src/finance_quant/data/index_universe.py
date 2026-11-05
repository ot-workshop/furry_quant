"""Historical index snapshots with explicit, conservative availability dates."""

from __future__ import annotations

import re

import pandas as pd


def normalize_memberships(raw, calendar, index_code="399300.SZ", expected_count=300):
    required = {"index_code", "con_code", "trade_date", "weight"}
    if not required.issubset(raw) or raw.empty:
        raise ValueError("指数历史成分响应为空或字段不完整")
    frame = raw.copy()
    if not frame.index_code.eq(index_code).all():
        raise ValueError("指数代码不匹配")
    if not frame.con_code.map(lambda c: isinstance(c, str) and bool(re.fullmatch(r"\d{6}\.(SH|SZ)", c))).all():
        raise ValueError("指数成分包含无效股票代码")
    frame["snapshot_date"] = pd.to_datetime(frame.trade_date, format="%Y%m%d", errors="raise")
    if frame.duplicated(["snapshot_date", "con_code"]).any():
        raise ValueError("指数成分重复")
    frame["weight"] = pd.to_numeric(frame.weight, errors="raise")
    if frame.weight.isna().any() or frame.weight.le(0).any():
        raise ValueError("指数权重无效")
    sessions = pd.DatetimeIndex(calendar.loc[calendar.is_open, "date"]).sort_values()
    out = []
    dates = sorted(frame.snapshot_date.unique())
    effective = {}
    for value in dates:
        day = pd.Timestamp(value)
        if day not in sessions:
            raise ValueError("指数快照日期不是已验证交易日")
        following = sessions[sessions > day]
        if following.empty:
            raise ValueError("指数快照缺少后续交易日边界")
        group = frame.loc[frame.snapshot_date.eq(day)]
        if len(group) != expected_count or abs(group.weight.sum() - 100) > 0.15:
            raise ValueError(f"{day.date()}成分数量或权重和不完整：{len(group)}只")
        effective[day] = following[0]
    for i, value in enumerate(dates):
        day = pd.Timestamp(value)
        group = frame.loc[frame.snapshot_date.eq(day)].copy()
        group["effective_from"] = effective[day]
        group["effective_until"] = effective[pd.Timestamp(dates[i + 1])] if i + 1 < len(dates) else pd.NaT
        out.append(group[["con_code", "snapshot_date", "effective_from", "effective_until", "weight"]])
    return pd.concat(out, ignore_index=True).rename(columns={"con_code": "code"})


def members_on(intervals, day):
    day = pd.Timestamp(day)
    selected = intervals.loc[
        (intervals.effective_from <= day) & (intervals.effective_until.isna() | (intervals.effective_until > day))
    ]
    return tuple(sorted(selected.code.unique()))


def historical_codes(intervals, start, end):
    selected = intervals.loc[
        (intervals.effective_from <= pd.Timestamp(end))
        & (intervals.effective_until.isna() | (intervals.effective_until > pd.Timestamp(start)))
    ]
    return tuple(sorted(selected.code.unique()))


def membership_mask(intervals, code, dates):
    dates = pd.DatetimeIndex(dates)
    mask = pd.Series(False, index=dates)
    for row in intervals.loc[intervals.code.eq(code)].itertuples():
        mask |= (dates >= row.effective_from) & (True if pd.isna(row.effective_until) else dates < row.effective_until)
    return mask.to_numpy()
