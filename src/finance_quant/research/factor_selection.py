"""Causal quality screening and exact low-redundancy factor subset selection."""

from __future__ import annotations

import math
import warnings
from dataclasses import asdict, dataclass
from datetime import date
from threading import Lock

import numpy as np
import pandas as pd
from scipy.optimize import Bounds, LinearConstraint, milp

from finance_quant.research.evaluation_analysis import _stats

ALGORITHM = "quality-dominance-milp-v1"
SOLVER_LOCK = Lock()


def _date(value):
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError("筛选日期须为 YYYY-MM-DD") from None
    if parsed.isoformat() != value:
        raise ValueError("筛选日期须为 YYYY-MM-DD")
    return value


@dataclass(frozen=True)
class SelectionParameters:
    horizon: int
    cutoff: str
    factor_count: int = 3
    min_ic: float = 0.0
    min_ir: float = 0.0
    max_correlation: float = 0.6
    min_dates: int = 20
    start: str | None = None

    def __post_init__(self):
        for key, lower, upper in (("horizon", 1, 252), ("factor_count", 1, 20), ("min_dates", 2, 25200)):
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
                raise ValueError(f"{key} 须为 {lower}–{upper} 的整数")
        for key, maximum in (("min_ic", 1), ("min_ir", 1e6), ("max_correlation", 1)):
            value = getattr(self, key)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not 0 <= value <= maximum
            ):
                raise ValueError(f"{key} 须为 0–{maximum} 的有限数值")
            # JSON/JavaScript serializes 0.0 as 0; equivalent thresholds need one reference identity.
            object.__setattr__(self, key, float(value))
        _date(self.cutoff)
        if self.start is not None and _date(self.start) > self.cutoff:
            raise ValueError("统计开始日期不能晚于推荐截止日")

    def as_dict(self):
        return asdict(self)


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _quality(result, parameters):
    candidates = []
    for key, factor in sorted(result["factors"].items()):
        if key == "composite":
            continue
        diagnostic = factor["horizons"].get(str(parameters.horizon))
        if diagnostic is None:
            raise ValueError("该评估没有所选持有期")
        daily = diagnostic["daily"]
        signals = [
            row
            for row in daily
            if (not parameters.start or row["date"] >= parameters.start) and row["date"] <= parameters.cutoff
        ]
        matured = [row for row in signals if row.get("exit_date") is not None and row["exit_date"] <= parameters.cutoff]
        values = pd.Series(
            [row.get("rank_ic") if _finite(row.get("rank_ic")) else None for row in matured], dtype=float
        )
        stats = _stats(values)
        reasons = []
        if stats["valid_dates"] < parameters.min_dates:
            reasons.append(f"已实现标签的有效日期不足：{stats['valid_dates']}/{parameters.min_dates}")
        if stats["mean"] is None or stats["mean"] < parameters.min_ic:
            reasons.append("平均 Rank IC 未达到门槛")
        if stats["ir"] is None:
            reasons.append("IR 未定义（有效样本不足或标准差为零）")
        elif stats["ir"] < parameters.min_ir:
            reasons.append("Rank IC IR 未达到门槛")
        candidates.append(
            {
                "key": key,
                "rank_ic": stats,
                "last_label_exit": max(
                    (row["exit_date"] for row in matured if _finite(row.get("rank_ic"))), default=None
                ),
                "unmatured_dates": len(signals) - len(matured),
                "status": "quality_excluded" if reasons else "eligible",
                "reasons": reasons,
            }
        )
    return candidates


def _conflicts(result, parameters, keys):
    by_pair = {
        (min(pair["first"], pair["second"]), max(pair["first"], pair["second"])): pair
        for pair in result["correlations"]["pairs"]
    }
    pairs, edges = [], set()
    for index, first in enumerate(keys):
        for second in keys[index + 1 :]:
            source = by_pair.get((first, second), {})
            values = [
                abs(row["rank_correlation"])
                for row in source.get("daily", [])
                if (not parameters.start or row["date"] >= parameters.start)
                and row["date"] <= parameters.cutoff
                and _finite(row.get("rank_correlation"))
            ]
            correlation = float(np.mean(values)) if values else None
            insufficient = len(values) < parameters.min_dates
            conflict = insufficient or correlation is None or correlation > parameters.max_correlation
            if conflict:
                edges.add((first, second))
            pairs.append(
                {
                    "first": first,
                    "second": second,
                    "mean_absolute": correlation,
                    "valid_dates": len(values),
                    "conflict": conflict,
                    "reason": "相关性样本不足" if insufficient else "超过相关性上限" if conflict else None,
                }
            )
    return pairs, edges


def _dominance(candidates, edges):
    """Safe graph dominance: an adjacent, no-worse-quality substitute has fewer conflicts."""
    active = {row["key"] for row in candidates if row["status"] == "eligible"}
    quality = {row["key"]: row["rank_ic"]["mean"] for row in candidates if row["key"] in active}
    neighbors: dict[str, set[str]] = {key: set() for key in active}
    for first, second in edges:
        neighbors[first].add(second)
        neighbors[second].add(first)
    removed = {}
    for weaker in sorted(active, key=lambda key: (quality[key], key), reverse=False):
        for stronger in sorted(active, key=lambda key: (-quality[key], key)):
            if stronger == weaker or stronger not in neighbors[weaker]:
                continue
            if quality[stronger] < quality[weaker] or (quality[stronger] == quality[weaker] and stronger > weaker):
                continue
            if (neighbors[stronger] & active) - {weaker} <= (neighbors[weaker] & active) - {stronger}:
                active.remove(weaker)
                removed[weaker] = stronger
                break
    return sorted(active), removed


def _solve(keys, quality, edges, count=None):
    if not keys:
        return None
    rows, lower, upper = [], [], []
    if count is not None:
        rows.append(np.ones(len(keys)))
        lower.append(count)
        upper.append(count)
    positions = {key: index for index, key in enumerate(keys)}
    for first, second in sorted(edges):
        if first not in positions or second not in positions:
            continue
        row = np.zeros(len(keys))
        row[positions[first]] = row[positions[second]] = 1
        rows.append(row)
        lower.append(-np.inf)
        upper.append(1)
    constraints = LinearConstraint(np.asarray(rows), np.asarray(lower), np.asarray(upper)) if rows else None
    # HiGHS owns a process-global scheduler. Keep small web requests serial and single-threaded.
    with SOLVER_LOCK, warnings.catch_warnings():
        # SciPy forwards this supported HiGHS option while warning about its narrower own option list.
        warnings.filterwarnings("ignore", message="Unrecognized options detected:.*threads.*", category=RuntimeWarning)
        solved = milp(
            -np.asarray([quality[key] for key in keys]),
            integrality=np.ones(len(keys)),
            bounds=Bounds(0, 1),
            constraints=constraints,
            options={"time_limit": 5, "mip_rel_gap": 0.0, "threads": 1},
        )
    if solved.status == 2:
        return None
    if solved.status != 0 or solved.x is None:
        raise ValueError("组合求解尚未得到确定的最优解，请减少候选或稍后重试")
    selected = [key for key, value in zip(keys, solved.x, strict=True) if value > 0.5]
    if (count is not None and len(selected) != count) or any(
        first in selected and second in selected for first, second in edges
    ):
        raise ValueError("求解结果未满足组合约束，未生成推荐")
    return selected


def select_factors(result, parameters: SelectionParameters):
    if result.get("metadata", {}).get("end") and parameters.cutoff > result["metadata"]["end"]:
        raise ValueError("推荐截止日不能晚于该评估的结束日期")
    candidates = _quality(result, parameters)
    eligible = [row["key"] for row in candidates if row["status"] == "eligible"]
    pairs, edges = _conflicts(result, parameters, eligible)
    active, dominated = _dominance(candidates, edges)
    quality = {row["key"]: row["rank_ic"]["mean"] for row in candidates if row["key"] in active}
    chosen = _solve(active, quality, edges, parameters.factor_count) if len(active) >= parameters.factor_count else None
    maximum = None
    if chosen is None:
        largest = _solve(active, dict.fromkeys(active, 1), edges)
        maximum = len(largest or [])
    for row in candidates:
        key = row["key"]
        if key in dominated:
            row.update(status="dominated", reasons=[f"被 {dominated[key]} 支配：IC 不低且可搭配范围不小"])
        elif chosen is not None and key in chosen:
            row.update(status="selected", reasons=["质量及相关性约束通过，MILP 入选"])
        elif row["status"] == "eligible":
            row.update(
                status="not_selected",
                reasons=["可用，但未进入本次最优组合"] if chosen else ["当前约束下无法选满所需数量"],
            )
    selected_pairs = [row for row in pairs if row["first"] in (chosen or []) and row["second"] in (chosen or [])]
    values = [row["mean_absolute"] for row in selected_pairs]
    return {
        "algorithm": ALGORITHM,
        "parameters": parameters.as_dict(),
        "status": "optimal" if chosen else "infeasible",
        "selected_keys": chosen or [],
        "candidates": candidates,
        "pairs": pairs,
        "max_feasible_count": maximum,
        "stages": {
            "input": len(candidates),
            "quality_passed": len(eligible),
            "dominance_removed": len(dominated),
            "milp_candidates": len(active),
        },
        "objective_sum_ic": float(sum(quality[key] for key in chosen)) if chosen else None,
        "max_selected_correlation": max(values) if values else None,
        "mean_selected_correlation": float(np.mean(values)) if values else None,
        "message": "已找到满足条件的最优因子子集"
        if chosen
        else f"当前门槛和相关性限制下，最多可选择 {maximum} 个因子，无法选满 {parameters.factor_count} 个",
        "methodology": [
            "质量指标为已实现收益标签的 Rank IC 与未年化 IR；仅使用 exit_date 不晚于推荐截止日的记录。",
            "相关性为截止日前每日绝对 Spearman 的平均值；相关样本不足的因子对不能同时入选。",
            "先做质量预筛，再做保持最优解的冲突图支配剪枝，最后用 HiGHS MILP 最大化所选单因子的平均 IC。",
            "候选方向沿用原因子定义，权重先等权；单因子 IC 的加总不是实际组合 IC。",
            "来源评估可能使用所有原候选共同有效的股票交集；新组合的实际覆盖、IC 和收益需重新计算。",
        ],
    }
