"""Bound queued warehouse experiments before reading stock quotes or publishing results."""

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from finance_quant.application import market_download
from finance_quant.checksums import file_sha256, sha256_bytes
from finance_quant.data.index_universe import historical_codes
from finance_quant.data.market_universe import SCHEMA, market_roots
from finance_quant.data.warehouse_files import warehouse_files
from finance_quant.research.experiments import ExperimentPlan
from finance_quant.research.universe import parse_filters

WAREHOUSE_SCHEMAS = {
    "market_warehouse": ("market", SCHEMA),
    "index_warehouse": ("csi300", "historical-index-warehouse-v1"),
}
MAX_STOCKS = 384
MAX_CALENDAR_DAYS = 366
MAX_SESSIONS = 243
MAX_PLAN_WEIGHT = 60
# 128 stocks, 240 trading days and three warmup days, two one-factor candidates in common mode.
MAX_WEIGHTED_SESSIONS = 622_080
MAX_MANIFEST_BYTES = 4 * 2**20
MAX_WAREHOUSE_FILES = 10_000
MAX_WAREHOUSE_BYTES = 512 * 2**20
MAX_INPUT_ROWS = 1_000_000
MAX_INPUT_BYTES = 256 * 2**20


def _limit(label: str, actual: int, maximum: int, reduction: str) -> None:
    if actual > maximum:
        raise ValueError(f"仓库方案比较{label}超限（{actual}，上限 {maximum}），请{reduction}")


def _days(plan: ExperimentPlan) -> tuple[str, str]:
    return plan.periods[0].start, plan.periods[-1].end


def _warmup(data: dict, plan: ExperimentPlan) -> int:
    filters = parse_filters(data["filters"]) if "filters" in data else None
    amount = filters.amount_window - 1 if filters is not None and filters.min_avg_amount is not None else 0
    return max(amount, max(spec.lookback for candidate in plan.candidates for spec in candidate.factors.specs)) + 1


def _plan_weight(plan: ExperimentPlan) -> int:
    # Independent: validation, sample discovery, one result. Common adds sample re-ranking
    # and a paired result. Splits use up to two preflights and three separate intervals.
    passes = 3 if plan.comparison and plan.comparison.sample_mode != "common" else 5
    return passes * sum(1 + len(candidate.factors.specs) for candidate in plan.candidates)


def check_submission(data: dict, plan: ExperimentPlan) -> None:
    start, end = map(date.fromisoformat, _days(plan))
    _limit("自然日数", (end - start).days + 1, MAX_CALENDAR_DAYS, "缩短日期区间")
    _limit("含预热的最少交易日数", _warmup(data, plan) + 1, MAX_SESSIONS, "缩短因子或筛选的预热窗口")
    _limit("方案权重", _plan_weight(plan), MAX_PLAN_WEIGHT, "减少候选方案或因子，或切换到独立样本")
    if data.get("codes") is not None:
        _limit("显式股票数", len(data["codes"]), MAX_STOCKS, "减少选股数量")


def _pinned_manifest(data_root: Path, data: dict, execution: dict | None):
    mode = data["mode"]
    prefix, schema = WAREHOUSE_SCHEMAS[mode]
    roots = market_roots(data_root) if mode == "market_warehouse" else market_download.warehouse_roots(data_root)
    if execution is not None:
        pinned = Path(execution["warehouse_dir"]).resolve()
        if pinned not in roots or execution["mode"] != mode:
            raise ValueError("已排队任务的历史仓库已不可用，请刷新数据后重新提交")
        roots = [pinned]
    for root in roots:
        try:
            with (root / "manifest.json").open("rb") as source:
                raw = source.read(MAX_MANIFEST_BYTES + 1)
        except OSError:
            continue
        if len(raw) > MAX_MANIFEST_BYTES:
            continue
        digest = sha256_bytes(raw)
        if data["warehouse_id"] != f"{prefix}-{digest[:20]}":
            continue
        if execution is not None and digest != execution["manifest_sha256"]:
            break
        return root, digest, raw, schema
    raise ValueError("所选仓库版本已变化、不可用或清单超过 4 MiB，请刷新数据并选择较小的仓库")


def _parquet_size(paths, *, initial=(0, 0)):
    rows, decoded_bytes = initial
    for path in paths:
        metadata = pq.read_metadata(path)
        rows += metadata.num_rows
        decoded_bytes += sum(metadata.row_group(i).total_byte_size for i in range(metadata.num_row_groups))
        reduction = "选择历史跨度较短的仓库或减少股票数"
        _limit("输入行数", rows, MAX_INPUT_ROWS, reduction)
        _limit("Parquet 解压字节数", decoded_bytes, MAX_INPUT_BYTES, reduction)
    return rows, decoded_bytes


def check_warehouse(data_root: Path, data: dict, plan: ExperimentPlan, *, execution: dict | None = None):
    """Resolve the submitted content ID and admit only bounded reads from that version."""
    check_submission(data, plan)
    root, digest, raw, schema = _pinned_manifest(data_root, data, execution)
    manifest = json.loads(raw)
    if manifest.get("schema_version") != schema or not manifest.get("assets"):
        raise ValueError("历史仓库发布不完整，请选择已发布版本")
    refs, assets = manifest["reference_files"], manifest["assets"]
    is_market = data["mode"] == "market_warehouse"
    required = {"calendar.parquet", "instruments.parquet", "benchmark.parquet"}
    if not is_market:
        required.add("memberships.parquet")
    if required - refs.keys():
        raise ValueError("历史仓库缺少已冻结的参考文件，请重新发布该版本")
    _limit("整仓文件数", len(refs) + len(assets) + 1, MAX_WAREHOUSE_FILES, "选择较小的仓库")
    files = {relative: (path, sha) for path, relative, sha in warehouse_files(root, manifest)}
    total_bytes = len(raw) + sum(path.stat().st_size for path, _ in files.values())
    _limit("整仓冻结字节数", total_bytes, MAX_WAREHOUSE_BYTES, "选择较小的仓库")
    reference_paths = [files[Path("reference") / name][0] for name in refs]
    reference_size = _parquet_size(reference_paths)

    def reference(name):
        path, sha = files[Path("reference") / name]
        if file_sha256(path) != sha:
            raise ValueError(f"历史仓库参考文件 {name} 已变化，请刷新数据后重新提交")
        return pd.read_parquet(path)

    calendar = reference("calendar.parquet")
    sessions = pd.DatetimeIndex(calendar.loc[calendar.is_open, "date"])
    if not sessions.is_unique or not sessions.is_monotonic_increasing:
        raise ValueError("历史仓库交易日历顺序或日期无效，请重新发布该版本")
    start, end = _days(plan)
    begin, finish = pd.Timestamp(start), pd.Timestamp(end)
    if not (manifest["start"] <= start <= end <= manifest["end"] and begin in sessions and finish in sessions):
        raise ValueError("回测区间须在所选仓库内，且起止为交易日")
    warmup = _warmup(data, plan)
    first, last = sessions.get_loc(begin), sessions.get_loc(finish)
    if first < warmup or sessions[first - warmup] < pd.Timestamp(manifest["data_start"]):
        raise ValueError(f"请选择有至少 {warmup} 个预热交易日的回测起点")
    trading_days = last - first + 1 + warmup
    _limit("含预热交易日数", trading_days, MAX_SESSIONS, "缩短回测区间或预热窗口")

    if is_market:
        codes = data.get("codes")
        pool = assets.keys() if codes is None else codes
        pool_label = "展开后的股票数"
        reduction = "显式选择较小的股票池" if codes is None else "减少选股数量"
    else:
        intervals = reference("memberships.parquet")
        pool = historical_codes(intervals, begin, finish)
        pool_label, reduction = "区间历史成分股票数", "缩短指数比较区间"
    if set(pool) - assets.keys():
        raise ValueError("所选股票超出冻结仓库的行情覆盖范围，请缩小选股范围")
    _limit(pool_label, len(pool), MAX_STOCKS, reduction)
    _limit(
        "加权股票交易日工作量",
        len(pool) * trading_days * _plan_weight(plan),
        MAX_WEIGHTED_SESSIONS,
        "减少股票、交易日、候选方案或因子，或切换到独立样本",
    )
    _parquet_size((files[Path(assets[code]["file"])][0] for code in pool), initial=reference_size)
    return root, digest
