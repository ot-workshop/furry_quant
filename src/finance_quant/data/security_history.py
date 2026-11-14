"""Dated security names used as historical ST evidence, never current-name backfills."""

import pandas as pd

from finance_quant.data.symbols import to_qlib

COLUMNS = ["instrument", "name", "start_date", "end_date", "ann_date"]
ST_POLICY = "历史名称识别ST；变更开始日与已提供公告日的较晚日期后才使用，缺失与冲突为未知；非官方逐日ST认证"


def normalize_names(raw, code):
    if raw.empty:
        return pd.DataFrame({name: pd.Series(dtype="object") for name in COLUMNS})
    if not {"ts_code", "name", "start_date", "end_date", "ann_date"}.issubset(raw):
        raise ValueError(f"{code}历史名称字段不完整")
    if not raw.ts_code.eq(code).all() or len(raw) >= 5000:
        raise ValueError(f"{code}历史名称代码不一致或响应可能截断")
    frame = raw.assign(instrument=to_qlib(code))[COLUMNS].copy()
    for column in ("start_date", "end_date", "ann_date"):
        frame[column] = pd.to_datetime(frame[column].replace("", None), format="%Y%m%d", errors="raise")
    if frame.start_date.isna().any() or frame.name.isna().any() or frame.end_date.lt(frame.start_date).any():
        raise ValueError(f"{code}历史名称缺少起点或区间无效")
    return frame.drop_duplicates().reset_index(drop=True)


def name_states(dates, history):
    """Return nullable booleans; a date-only event cannot enter that day's signal."""
    target = pd.DataFrame({"date": pd.DatetimeIndex(dates)})
    if history.empty:
        return pd.Series(pd.NA, index=target.index, dtype="boolean")
    history = history.copy()
    history["visible_from"] = history[["start_date", "ann_date"]].max(axis=1) + pd.Timedelta(days=1)
    history["is_st"] = history.name.str.upper().str.replace(" ", "").str.match(r"^(?:S\*?ST|\*?ST)").astype("boolean")
    history = history.sort_values(["visible_from", "start_date"])
    # A later-published correction of an older interval cannot replace a newer known name.
    history = history.loc[history.start_date.ge(history.start_date.cummax())]
    newest = history.groupby("visible_from").start_date.transform("max")
    history = history.loc[history.start_date.eq(newest)].copy()
    conflicts = history.groupby("visible_from").is_st.nunique()
    history.loc[history.visible_from.isin(conflicts[conflicts > 1].index), "is_st"] = pd.NA
    history = history.drop_duplicates("visible_from", keep="last")
    joined = pd.merge_asof(target, history, left_on="date", right_on="visible_from", direction="backward")
    # end_date is often filled retrospectively by the next change. Only a visible
    # successor changes the last known state; a future closing date is not evidence.
    return joined.is_st.astype("boolean")
