"""Read-only factor evidence diagnostics and Newey-West Rank IC intervals.

This module lives in ``web/`` so it never alters computation provenance hashes
while projecting frozen evaluation and factor artifacts.
"""

from __future__ import annotations

import math
from datetime import date

Z_CRIT_95 = 1.959963984540054
MIN_VALID_DATES = 20

EXCLUSION_LABELS = {
    "insufficient_cross_section": "有效截面不足",
    "constant_score": "因子评分为常数",
    "constant_return": "未来收益为常数",
    "unmatured": "截止日时尚未到期",
    "missing_exit_date": "缺少离场日记录",
    "missing_daily_row": "缺少当日诊断记录",
}

LABEL_EXCLUSION_LABELS = {
    "outside_evaluation_end": "超出评估结束日",
    "missing_entry_or_exit": "缺少入场或离场行情",
    "non_trading_entry_or_exit": "入场或离场日停牌",
}


def _finite(value) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def _parse_date(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label}须为 YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{label}须为 YYYY-MM-DD") from None
    if parsed.isoformat() != value:
        raise ValueError(f"{label}须为 YYYY-MM-DD")
    return value


def _validate_inputs(
    result: dict,
    *,
    horizon: int,
    factor: str,
    start: str | None,
    end: str | None,
    cutoff: str | None,
    lag: int | None,
) -> tuple[str | None, str | None, str, int]:
    if isinstance(horizon, bool) or not isinstance(horizon, int) or not 1 <= horizon <= 252:
        raise ValueError("horizon 须为 1–252 的整数")
    if factor not in result.get("factors", {}) or str(horizon) not in result["factors"][factor].get("horizons", {}):
        raise ValueError("所选因子或持有期不存在")
    eff_start = _parse_date(start, "开始日期")
    eff_end = _parse_date(end, "结束日期")
    if eff_start and eff_end and eff_start > eff_end:
        raise ValueError("开始日期不能晚于结束日期")
    eff_cutoff = _parse_date(cutoff, "截止日期")
    if eff_cutoff is None:
        raw_end = result.get("metadata", {}).get("end")
        try:
            eff_cutoff = _parse_date(raw_end, "评估结束日期")
        except ValueError:
            eff_cutoff = None
        if eff_cutoff is None:
            raise ValueError("评估结果缺少结束日期，请显式传入 cutoff")
    eff_lag = (horizon - 1) if lag is None else lag
    if isinstance(eff_lag, bool) or not isinstance(eff_lag, int) or not 0 <= eff_lag <= 251:
        raise ValueError("lag 须为 0–251 的整数")
    return eff_start, eff_end, eff_cutoff, eff_lag


def _project_raw_availability(
    store,
    result: dict,
    *,
    factor: str,
    dates: list[str],
) -> dict:
    metadata = result.get("metadata", {})
    factor_run_id = metadata.get("factor_run_id")
    if not isinstance(factor_run_id, str) or not factor_run_id:
        return {
            "available": False,
            "reason": "未关联冻结因子批次（旧结果或缺少 factor_run_id）",
            "universe_stock_days": None,
            "eligible_stock_days": None,
            "raw_valid_stock_days": None,
            "raw_missing_stock_days": None,
            "imputed_stock_days": None,
            "not_applicable_stock_days": None,
            "not_applicable_scored_stock_days": None,
            "missing_policy": None,
            "component_imputations": [],
        }
    try:
        factor_doc = store.get("factors", factor_run_id, hydrate=False)
    except (FileNotFoundError, ValueError):
        return {
            "available": False,
            "reason": "关联因子批次不可读取，原始观测与填补数未知",
            "universe_stock_days": None,
            "eligible_stock_days": None,
            "raw_valid_stock_days": None,
            "raw_missing_stock_days": None,
            "imputed_stock_days": None,
            "not_applicable_stock_days": None,
            "not_applicable_scored_stock_days": None,
            "missing_policy": None,
            "component_imputations": [],
        }
    availability = factor_doc.get("metadata", {}).get("availability_by_date")
    if not isinstance(availability, dict) or not availability:
        return {
            "available": False,
            "reason": "因子批次未保存逐日可用性明细（旧结果不回填）",
            "universe_stock_days": None,
            "eligible_stock_days": None,
            "raw_valid_stock_days": None,
            "raw_missing_stock_days": None,
            "imputed_stock_days": None,
            "not_applicable_stock_days": None,
            "not_applicable_scored_stock_days": None,
            "missing_policy": None,
            "component_imputations": [],
        }

    matched_days = 0
    universe_total = 0
    eligible_total = 0
    raw_valid = 0
    raw_missing = 0
    imputed_total = 0
    has_imputed = False
    not_applicable_total = 0
    not_applicable_scored_total = 0
    has_not_applicable = False
    missing_policy: str | None = None
    component_acc: dict[str, dict] = {}

    for day in dates:
        day_info = availability.get(day)
        if not isinstance(day_info, dict):
            continue
        matched_days += 1
        universe_total += int(day_info.get("universe", 0))
        eligible_total += int(day_info.get("eligible", 0))
        factors_map = day_info.get("factors", {})
        if not isinstance(factors_map, dict):
            continue
        if factor != "composite":
            entry = factors_map.get(factor)
            if isinstance(entry, dict):
                raw_valid += int(entry.get("valid", 0))
                raw_missing += int(entry.get("missing", 0))
                if "imputed" in entry:
                    has_imputed = True
                    imputed_total += int(entry.get("imputed", 0))
                if "not_applicable" in entry:
                    has_not_applicable = True
                    not_applicable_total += int(entry.get("not_applicable", 0))
                    not_applicable_scored_total += int(entry.get("not_applicable_scored", 0))
                if entry.get("missing_policy"):
                    missing_policy = str(entry["missing_policy"])
        else:
            for key, entry in factors_map.items():
                if not isinstance(entry, dict):
                    continue
                bucket = component_acc.setdefault(
                    key,
                    {
                        "factor": key,
                        "raw_valid_stock_days": 0,
                        "raw_missing_stock_days": 0,
                        "imputed_stock_days": 0 if "imputed" in entry else None,
                        "not_applicable_scored_stock_days": 0 if "not_applicable_scored" in entry else None,
                        "missing_policy": entry.get("missing_policy"),
                    },
                )
                bucket["raw_valid_stock_days"] += int(entry.get("valid", 0))
                bucket["raw_missing_stock_days"] += int(entry.get("missing", 0))
                if "imputed" in entry:
                    has_imputed = True
                    imputed_total += int(entry.get("imputed", 0))
                    bucket["imputed_stock_days"] = (bucket["imputed_stock_days"] or 0) + int(entry.get("imputed", 0))
                if "not_applicable_scored" in entry:
                    has_not_applicable = True
                    not_applicable_scored_total += int(entry.get("not_applicable_scored", 0))
                    bucket["not_applicable_scored_stock_days"] = (
                        bucket["not_applicable_scored_stock_days"] or 0
                    ) + int(entry.get("not_applicable_scored", 0))

    if matched_days == 0:
        return {
            "available": False,
            "reason": "所选信号区间在关联因子批次中没有可用性记录",
            "universe_stock_days": None,
            "eligible_stock_days": None,
            "raw_valid_stock_days": None,
            "raw_missing_stock_days": None,
            "imputed_stock_days": None,
            "not_applicable_stock_days": None,
            "not_applicable_scored_stock_days": None,
            "missing_policy": None,
            "component_imputations": [],
        }

    return {
        "available": True,
        "factor_run_id": factor_run_id,
        "matched_signal_dates": matched_days,
        "universe_stock_days": universe_total,
        "eligible_stock_days": eligible_total,
        "raw_valid_stock_days": None if factor == "composite" else raw_valid,
        "raw_missing_stock_days": None if factor == "composite" else raw_missing,
        "imputed_stock_days": imputed_total if has_imputed else None,
        "not_applicable_stock_days": not_applicable_total if has_not_applicable else None,
        "not_applicable_scored_stock_days": not_applicable_scored_total if has_not_applicable else None,
        "missing_policy": missing_policy,
        "component_imputations": list(component_acc.values()) if factor == "composite" else [],
    }


def _common_sample_info(store, result: dict) -> dict:
    metadata = result.get("metadata", {})
    scoring_sample = metadata.get("provenance", {}).get("scoring_sample")
    if not isinstance(scoring_sample, dict) and isinstance(metadata.get("factor_run_id"), str):
        try:
            factor_doc = store.get("factors", metadata["factor_run_id"], hydrate=False)
            scoring_sample = factor_doc.get("metadata", {}).get("provenance", {}).get("scoring_sample")
        except (FileNotFoundError, ValueError):
            scoring_sample = None
    if isinstance(scoring_sample, dict):
        return {
            "available": True,
            "policy": scoring_sample.get("policy"),
            "sha256": scoring_sample.get("sha256"),
            "label": "已冻结共同样本",
        }
    return {
        "available": False,
        "policy": None,
        "sha256": None,
        "label": "未知（无冻结共同样本证据）",
    }


def _newey_west_bartlett(values: list[float | None], lag: int) -> dict:
    """Compute Bartlett-kernel Newey-West SE on ordered frozen signal positions.

    Gaps enter covariance sums as z_t = 0 but are never treated as observations.
    """
    valid_values = [v for v in values if v is not None]
    n = len(valid_values)
    min_dates = max(MIN_VALID_DATES, 2 * (lag + 1))
    if n == 0:
        return {
            "n": 0,
            "mean": None,
            "sample_std": None,
            "se": None,
            "ci_low": None,
            "ci_high": None,
            "variance": None,
            "lag_pairs": 0,
            "min_dates_required": min_dates,
            "variance_status": "no_observations",
        }

    mean = sum(valid_values) / n
    sample_std = math.sqrt(sum((v - mean) ** 2 for v in valid_values) / (n - 1)) if n >= 2 else None
    z = [0.0 if v is None else (v - mean) for v in values]
    valid_mask = [v is not None for v in values]
    m = len(values)
    lag_pairs = sum(1 for k in range(1, lag + 1) for t in range(k, m) if valid_mask[t] and valid_mask[t - k])

    if n < min_dates:
        return {
            "n": n,
            "mean": mean,
            "sample_std": sample_std,
            "se": None,
            "ci_low": None,
            "ci_high": None,
            "variance": None,
            "lag_pairs": lag_pairs,
            "min_dates_required": min_dates,
            "variance_status": "insufficient_observations",
        }

    gamma_0 = sum(val * val for val in z)
    if gamma_0 <= 1e-24:
        return {
            "n": n,
            "mean": mean,
            "sample_std": 0.0 if sample_std is not None else None,
            "se": None,
            "ci_low": None,
            "ci_high": None,
            "variance": None,
            "lag_pairs": lag_pairs,
            "min_dates_required": min_dates,
            "variance_status": "constant_or_zero_variance",
        }

    total_sum = gamma_0
    for k in range(1, lag + 1):
        weight = 1.0 - k / (lag + 1)
        cross = sum(z[t] * z[t - k] for t in range(k, m))
        total_sum += 2.0 * weight * cross

    if not math.isfinite(total_sum) or total_sum <= 1e-24:
        return {
            "n": n,
            "mean": mean,
            "sample_std": sample_std,
            "se": None,
            "ci_low": None,
            "ci_high": None,
            "variance": None,
            "lag_pairs": lag_pairs,
            "min_dates_required": min_dates,
            "variance_status": "degenerate_variance",
        }

    variance = total_sum / (n * (n - 1))
    se = math.sqrt(variance)
    return {
        "n": n,
        "mean": mean,
        "sample_std": sample_std,
        "se": se,
        "ci_low": mean - Z_CRIT_95 * se,
        "ci_high": mean + Z_CRIT_95 * se,
        "variance": variance,
        "lag_pairs": lag_pairs,
        "min_dates_required": min_dates,
        "variance_status": "ready",
    }


def evaluation_evidence(
    store,
    result: dict,
    *,
    horizon: int,
    factor: str = "composite",
    start: str | None = None,
    end: str | None = None,
    cutoff: str | None = None,
    lag: int | None = None,
) -> dict:
    """Build read-only factor evidence summary and Newey-West Rank IC interval."""
    eff_start, eff_end, eff_cutoff, eff_lag = _validate_inputs(
        result,
        horizon=horizon,
        factor=factor,
        start=start,
        end=end,
        cutoff=cutoff,
        lag=lag,
    )
    default_lag = horizon - 1
    min_dates = max(MIN_VALID_DATES, 2 * (eff_lag + 1))

    signal_cov = result.get("signal_coverage")
    if not isinstance(signal_cov, list) or not signal_cov:
        return {
            "factor": factor,
            "horizon": horizon,
            "status": "missing_frozen_calendar",
            "status_label": "缺少冻结信号日历",
            "status_reason": "旧评估或当前产物缺少冻结的 signal_coverage 交易日历，无法对齐滞后位置。",
            "range": {
                "start": eff_start,
                "end": eff_end,
                "cutoff": eff_cutoff,
                "latest_exit_date": None,
            },
            "lag": {
                "method": "newey_west_bartlett",
                "lag": eff_lag,
                "default_lag": default_lag,
                "is_shorter_than_default": eff_lag < default_lag,
                "min_dates_required": min_dates,
                "finite_sample_correction": "n*(n-1)",
                "confidence_level": 0.95,
                "z_critical": Z_CRIT_95,
            },
            "counts": {
                "signal_dates": 0,
                "valid_rank_ic_dates": 0,
                "matured_dates": 0,
                "unmatured_dates": 0,
                "missing_exit_date_dates": 0,
                "excluded_matured_dates": 0,
                "missing_daily_rows": 0,
                "lag_pairs": 0,
                "paired_observations_on_valid_ic_days": 0,
            },
            "rank_ic": {
                "mean": None,
                "sample_std": None,
                "se": None,
                "ci_low": None,
                "ci_high": None,
                "variance": None,
            },
            "exclusions": {"ic": [], "labels": []},
            "label_coverage": None,
            "raw_availability": _project_raw_availability(store, result, factor=factor, dates=[]),
            "common_sample": _common_sample_info(store, result),
            "financials": result.get("metadata", {}).get("financials"),
        }

    calendar = sorted(
        {row["signal_date"] for row in signal_cov if isinstance(row, dict) and isinstance(row.get("signal_date"), str)}
    )
    filtered_dates = [
        day for day in calendar if (eff_start is None or day >= eff_start) and (eff_end is None or day <= eff_end)
    ]
    range_start = filtered_dates[0] if filtered_dates else eff_start
    range_end = filtered_dates[-1] if filtered_dates else eff_end

    daily_rows = result["factors"][factor]["horizons"][str(horizon)].get("daily", [])
    daily_by_date = {
        row["date"]: row for row in daily_rows if isinstance(row, dict) and isinstance(row.get("date"), str)
    }

    values: list[float | None] = []
    valid_exit_dates: list[str] = []
    paired_obs_valid = 0
    matured_dates = 0
    unmatured_dates = 0
    missing_exit_date_dates = 0
    excluded_matured_dates = 0
    missing_daily_rows = 0
    ic_exclusion_counts: dict[str, int] = {}

    for day in filtered_dates:
        row = daily_by_date.get(day)
        if row is None:
            missing_daily_rows += 1
            ic_exclusion_counts["missing_daily_row"] = ic_exclusion_counts.get("missing_daily_row", 0) + 1
            values.append(None)
            continue
        exit_date = row.get("exit_date")
        if not isinstance(exit_date, str) or not exit_date:
            missing_exit_date_dates += 1
            ic_exclusion_counts["missing_exit_date"] = ic_exclusion_counts.get("missing_exit_date", 0) + 1
            values.append(None)
            continue
        if exit_date > eff_cutoff:
            unmatured_dates += 1
            ic_exclusion_counts["unmatured"] = ic_exclusion_counts.get("unmatured", 0) + 1
            values.append(None)
            continue

        matured_dates += 1
        val = _finite(row.get("rank_ic"))
        if val is not None:
            values.append(val)
            valid_exit_dates.append(exit_date)
            paired_obs_valid += int(row.get("observations") or 0)
        else:
            excluded_matured_dates += 1
            reason = str(row.get("ic_exclusion") or "unknown")
            ic_exclusion_counts[reason] = ic_exclusion_counts.get(reason, 0) + 1
            values.append(None)

    nw = _newey_west_bartlett(values, eff_lag)
    latest_exit_date = max(valid_exit_dates) if valid_exit_dates else None

    if not filtered_dates:
        status = "no_signal_dates"
        status_label = "所选区间无信号日"
        status_reason = "所选起止日期范围内没有冻结信号交易日。"
    elif nw["variance_status"] == "no_observations":
        status = "no_valid_observations"
        status_label = "无真实有效观测"
        if unmatured_dates > 0 and excluded_matured_dates == 0 and missing_exit_date_dates == 0:
            status_reason = (
                f"区间内 {len(filtered_dates)} 个信号日在截止日 {eff_cutoff} 前均未成熟，无有效 Rank IC 观测。"
            )
        elif missing_exit_date_dates > 0 and excluded_matured_dates == 0:
            status_reason = f"区间内 {missing_exit_date_dates} 个信号日缺少离场日记录，旧观测不推定已成熟。"
        else:
            status_reason = f"区间内 {len(filtered_dates)} 个信号日均未产出有效 Rank IC（有效截面不足或缺失）。"
    elif nw["variance_status"] == "insufficient_observations":
        status = "insufficient_observations"
        status_label = "有效日不足"
        status_reason = (
            f"已成熟有效 Rank IC 仅 {nw['n']} 日，低于滞后 L={eff_lag} 所需门槛 {min_dates} 日，暂不计算区间。"
        )
    elif nw["variance_status"] in {"constant_or_zero_variance", "degenerate_variance"}:
        status = "undefined_variance"
        status_label = "方差未定义"
        status_reason = (
            "有效 Rank IC 序列全为常数或零方差，区间未定义。"
            if nw["variance_status"] == "constant_or_zero_variance"
            else "Bartlett 核长程方差非正或退化，区间未定义。"
        )
    else:
        status = "ready"
        status_label = "可计算"
        status_reason = f"按冻结信号交易日位置与 Bartlett L={eff_lag} 计算，共 {nw['n']} 个已成熟有效日。"

    # Label coverage summary in [eff_start, eff_end]
    label_obj = result.get("labels", {}).get(str(horizon))
    label_coverage_summary = None
    label_exclusions_list = []
    if isinstance(label_obj, dict) and isinstance(label_obj.get("coverage"), list):
        filtered_set = set(filtered_dates)
        scored_sd = 0
        valid_sd = 0
        lbl_excl: dict[str, int] = {}
        for row in label_obj["coverage"]:
            if not isinstance(row, dict) or row.get("date") not in filtered_set:
                continue
            scored_sd += int(row.get("scored", 0))
            valid_sd += int(row.get("valid_labels", 0))
            for k, c in (row.get("exclusions") or {}).items():
                lbl_excl[str(k)] = lbl_excl.get(str(k), 0) + int(c)
        label_coverage_summary = {
            "scored_stock_days": scored_sd,
            "valid_stock_days": valid_sd,
            "excluded_stock_days": sum(lbl_excl.values()),
        }
        label_exclusions_list = [
            {"code": k, "label": LABEL_EXCLUSION_LABELS.get(k, k), "count": c}
            for k, c in sorted(lbl_excl.items(), key=lambda item: (-item[1], item[0]))
            if c > 0
        ]

    ic_exclusions_list = [
        {"code": k, "label": EXCLUSION_LABELS.get(k, k), "count": c}
        for k, c in sorted(ic_exclusion_counts.items(), key=lambda item: (-item[1], item[0]))
        if c > 0
    ]

    return {
        "factor": factor,
        "horizon": horizon,
        "status": status,
        "status_label": status_label,
        "status_reason": status_reason,
        "range": {
            "start": range_start,
            "end": range_end,
            "cutoff": eff_cutoff,
            "latest_exit_date": latest_exit_date,
        },
        "lag": {
            "method": "newey_west_bartlett",
            "lag": eff_lag,
            "default_lag": default_lag,
            "is_shorter_than_default": eff_lag < default_lag,
            "min_dates_required": min_dates,
            "finite_sample_correction": "n*(n-1)",
            "confidence_level": 0.95,
            "z_critical": Z_CRIT_95,
        },
        "counts": {
            "signal_dates": len(filtered_dates),
            "valid_rank_ic_dates": nw["n"],
            "matured_dates": matured_dates,
            "unmatured_dates": unmatured_dates,
            "missing_exit_date_dates": missing_exit_date_dates,
            "excluded_matured_dates": excluded_matured_dates,
            "missing_daily_rows": missing_daily_rows,
            "lag_pairs": nw["lag_pairs"],
            "paired_observations_on_valid_ic_days": paired_obs_valid,
        },
        "rank_ic": {
            "mean": nw["mean"],
            "sample_std": nw["sample_std"],
            "se": nw["se"],
            "ci_low": nw["ci_low"],
            "ci_high": nw["ci_high"],
            "variance": nw["variance"],
        },
        "exclusions": {
            "ic": ic_exclusions_list,
            "labels": label_exclusions_list,
        },
        "label_coverage": label_coverage_summary,
        "raw_availability": _project_raw_availability(store, result, factor=factor, dates=filtered_dates),
        "common_sample": _common_sample_info(store, result),
        "financials": result.get("metadata", {}).get("financials"),
    }
