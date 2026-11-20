"""Read-only factor evidence diagnostics and Newey-West Rank IC intervals.

This module lives in ``web/`` so it never alters computation provenance hashes
while projecting frozen evaluation and factor artifacts.
"""

from __future__ import annotations

import math
import re
from datetime import date

from finance_quant.data.temporal_audit import financial_audit_item

Z_CRIT_95 = 1.959963984540054
MIN_VALID_DATES = 20
SHA256_RE = re.compile(r"^(?:[0-9a-fA-F]{12}|[0-9a-fA-F]{64})$")

EXCLUSION_LABELS = {
    "insufficient_cross_section": "有效截面不足",
    "constant_score": "因子评分为常数",
    "constant_return": "未来收益为常数",
    "unmatured": "截止日时尚未到期",
    "missing_exit_date": "缺少离场日记录",
    "invalid_exit_date": "离场日格式或时序异常",
    "missing_daily_row": "缺少当日诊断记录",
}

FINANCIAL_STATUS_LABELS = {
    "guarded": "程序约束",
    "assumption": "保守假设",
    "model_assumption": "模拟边界",
    "selection_bias": "固定池限制",
    "unverified": "未认证",
    "diagnostic_only": "仅诊断",
    "not_used": "未使用",
    "missing_binding": "未绑定",
    "unknown": "无法判断",
    "not_modeled": "未建模",
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


def _unavailable_availability(reason: str, total_signal_dates: int | None) -> dict:
    return {
        "available": False,
        "coverage_status": "unavailable",
        "reason": reason,
        "matched_signal_dates": None if total_signal_dates is None else 0,
        "total_signal_dates": total_signal_dates,
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


def _opt_int(entry: dict, key: str) -> int | None:
    value = entry.get(key)
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _project_raw_availability(
    store,
    result: dict,
    *,
    factor: str,
    dates: list[str],
    calendar_known: bool = True,
) -> dict:
    total_days: int | None = len(dates) if calendar_known else None
    metadata = result.get("metadata", {})
    factor_run_id = metadata.get("factor_run_id")
    if not isinstance(factor_run_id, str) or not factor_run_id:
        return _unavailable_availability("未关联冻结因子批次（旧结果或缺少 factor_run_id）", total_days)
    try:
        factor_doc = store.get("factors", factor_run_id, hydrate=False)
    except (FileNotFoundError, ValueError):
        return _unavailable_availability("关联因子批次不可读取，原始观测与填补数未知", total_days)
    availability = factor_doc.get("metadata", {}).get("availability_by_date")
    if not isinstance(availability, dict) or not availability:
        return _unavailable_availability("因子批次未保存逐日可用性明细（旧结果不回填）", total_days)
    if not calendar_known or not dates:
        return _unavailable_availability("无信号日历或所选信号区间为空", total_days)

    day_matched = 0
    universe_total = 0
    universe_complete = True
    eligible_total = 0
    eligible_complete = True

    factor_matched_days = 0
    raw_valid = 0
    valid_complete = True
    raw_missing = 0
    missing_complete = True
    imputed_total = 0
    imputed_complete = True
    not_applicable_total = 0
    not_applicable_complete = True
    not_applicable_scored_total = 0
    not_applicable_scored_complete = True
    missing_policy: str | None = None
    policies_seen: set[str] = set()
    component_acc: dict[str, dict] = {}

    for day in dates:
        day_info = availability.get(day)
        if not isinstance(day_info, dict):
            universe_complete = False
            eligible_complete = False
            valid_complete = False
            missing_complete = False
            imputed_complete = False
            not_applicable_complete = False
            not_applicable_scored_complete = False
            continue

        day_matched += 1
        u_val = _opt_int(day_info, "universe")
        e_val = _opt_int(day_info, "eligible")
        if u_val is None:
            universe_complete = False
        else:
            universe_total += u_val
        if e_val is None:
            eligible_complete = False
        else:
            eligible_total += e_val

        factors_map = day_info.get("factors")
        if not isinstance(factors_map, dict):
            valid_complete = False
            missing_complete = False
            imputed_complete = False
            not_applicable_complete = False
            not_applicable_scored_complete = False
            continue

        if factor != "composite":
            entry = factors_map.get(factor)
            if not isinstance(entry, dict):
                valid_complete = False
                missing_complete = False
                imputed_complete = False
                not_applicable_complete = False
                not_applicable_scored_complete = False
                continue
            factor_matched_days += 1
            v = _opt_int(entry, "valid")
            m = _opt_int(entry, "missing")
            imp = _opt_int(entry, "imputed")
            na = _opt_int(entry, "not_applicable")
            nas = _opt_int(entry, "not_applicable_scored")
            if v is None:
                valid_complete = False
            else:
                raw_valid += v
            if m is None:
                missing_complete = False
            else:
                raw_missing += m
            if imp is None:
                imputed_complete = False
            else:
                imputed_total += imp
            if na is None:
                not_applicable_complete = False
            else:
                not_applicable_total += na
            if nas is None:
                not_applicable_scored_complete = False
            else:
                not_applicable_scored_total += nas
            if isinstance(entry.get("missing_policy"), str) and entry["missing_policy"]:
                policies_seen.add(entry["missing_policy"])
        else:
            for key, entry in factors_map.items():
                if not isinstance(entry, dict):
                    continue
                bucket = component_acc.setdefault(
                    key,
                    {
                        "factor": key,
                        "matched_days": 0,
                        "raw_valid_sum": 0,
                        "valid_complete": True,
                        "raw_missing_sum": 0,
                        "missing_complete": True,
                        "imputed_sum": 0,
                        "imputed_complete": True,
                        "na_sum": 0,
                        "na_complete": True,
                        "na_scored_sum": 0,
                        "na_scored_complete": True,
                        "policies": set(),
                    },
                )
                bucket["matched_days"] += 1
                v = _opt_int(entry, "valid")
                m = _opt_int(entry, "missing")
                imp = _opt_int(entry, "imputed")
                na = _opt_int(entry, "not_applicable")
                nas = _opt_int(entry, "not_applicable_scored")
                if v is None:
                    bucket["valid_complete"] = False
                else:
                    bucket["raw_valid_sum"] += v
                if m is None:
                    bucket["missing_complete"] = False
                else:
                    bucket["raw_missing_sum"] += m
                if imp is None:
                    bucket["imputed_complete"] = False
                else:
                    bucket["imputed_sum"] += imp
                if na is None:
                    bucket["na_complete"] = False
                else:
                    bucket["na_sum"] += na
                if nas is None:
                    bucket["na_scored_complete"] = False
                else:
                    bucket["na_scored_sum"] += nas
                if isinstance(entry.get("missing_policy"), str) and entry["missing_policy"]:
                    bucket["policies"].add(entry["missing_policy"])

    matched_for_factor = day_matched if factor == "composite" else factor_matched_days
    if matched_for_factor == 0:
        return _unavailable_availability("所选信号区间与因子在关联因子批次中没有可用性记录", total_days)

    full_days = len(dates)
    if factor == "composite":
        component_rows = []
        for b in component_acc.values():
            comp_full = b["matched_days"] == full_days
            policies = b["policies"]
            component_rows.append(
                {
                    "factor": b["factor"],
                    "matched_signal_dates": b["matched_days"],
                    "total_signal_dates": full_days,
                    "raw_valid_stock_days": b["raw_valid_sum"] if (comp_full and b["valid_complete"]) else None,
                    "raw_missing_stock_days": b["raw_missing_sum"] if (comp_full and b["missing_complete"]) else None,
                    "imputed_stock_days": b["imputed_sum"] if (comp_full and b["imputed_complete"]) else None,
                    "not_applicable_stock_days": b["na_sum"] if (comp_full and b["na_complete"]) else None,
                    "not_applicable_scored_stock_days": (
                        b["na_scored_sum"] if (comp_full and b["na_scored_complete"]) else None
                    ),
                    "missing_policy": next(iter(policies)) if len(policies) == 1 else None,
                }
            )
        is_complete = (
            day_matched == full_days
            and universe_complete
            and eligible_complete
            and bool(component_acc)
            and all(
                b["matched_days"] == full_days and b["valid_complete"] and b["missing_complete"]
                for b in component_acc.values()
            )
        )
        # Never sum cross-factor stock-days into a composite scalar total.
        return {
            "available": True,
            "coverage_status": "complete" if is_complete else "partial",
            "factor_run_id": factor_run_id,
            "matched_signal_dates": day_matched,
            "total_signal_dates": full_days,
            "universe_stock_days": universe_total if (day_matched == full_days and universe_complete) else None,
            "eligible_stock_days": eligible_total if (day_matched == full_days and eligible_complete) else None,
            "raw_valid_stock_days": None,
            "raw_missing_stock_days": None,
            "imputed_stock_days": None,
            "not_applicable_stock_days": None,
            "not_applicable_scored_stock_days": None,
            "missing_policy": None,
            "component_imputations": component_rows,
        }

    full_factor = factor_matched_days == full_days
    if len(policies_seen) == 1 and full_factor:
        missing_policy = next(iter(policies_seen))
    is_complete = full_factor and universe_complete and eligible_complete and valid_complete and missing_complete
    return {
        "available": True,
        "coverage_status": "complete" if is_complete else "partial",
        "factor_run_id": factor_run_id,
        "matched_signal_dates": factor_matched_days,
        "total_signal_dates": full_days,
        "universe_stock_days": universe_total if (day_matched == full_days and universe_complete) else None,
        "eligible_stock_days": eligible_total if (day_matched == full_days and eligible_complete) else None,
        "raw_valid_stock_days": raw_valid if (full_factor and valid_complete) else None,
        "raw_missing_stock_days": raw_missing if (full_factor and missing_complete) else None,
        "imputed_stock_days": imputed_total if (full_factor and imputed_complete) else None,
        "not_applicable_stock_days": not_applicable_total if (full_factor and not_applicable_complete) else None,
        "not_applicable_scored_stock_days": (
            not_applicable_scored_total if (full_factor and not_applicable_scored_complete) else None
        ),
        "missing_policy": missing_policy,
        "component_imputations": [],
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
        policy = scoring_sample.get("policy")
        sha256 = scoring_sample.get("sha256")
        if (
            isinstance(policy, str)
            and policy.strip()
            and isinstance(sha256, str)
            and SHA256_RE.fullmatch(sha256.strip())
        ):
            return {
                "available": True,
                "policy": policy.strip(),
                "sha256": sha256.strip().lower(),
                "label": "已冻结共同样本",
            }
    return {
        "available": False,
        "policy": None,
        "sha256": None,
        "label": "未知（无冻结共同样本证据）",
    }


def _financials_info(result: dict) -> dict | None:
    metadata = result.get("metadata", {})
    audit = metadata.get("temporal_audit")
    fin_item = None
    if isinstance(audit, dict) and isinstance(audit.get("items"), list):
        fin_item = next(
            (item for item in audit["items"] if isinstance(item, dict) and item.get("key") == "financials"),
            None,
        )
    if fin_item is None:
        fin_item = financial_audit_item(metadata)
    if isinstance(fin_item, dict) and isinstance(fin_item.get("status"), str):
        status = fin_item["status"]
        return {
            "status": status,
            "status_label": FINANCIAL_STATUS_LABELS.get(status, status),
            "label": str(fin_item.get("label") or "财务数据"),
            "detail": str(fin_item.get("detail") or ""),
        }
    return None


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
                "signal_dates": None,
                "valid_rank_ic_dates": None,
                "matured_dates": None,
                "unmatured_dates": None,
                "missing_exit_date_dates": None,
                "invalid_exit_date_dates": None,
                "excluded_matured_dates": None,
                "missing_daily_rows": None,
                "lag_pairs": None,
                "paired_observations_on_valid_ic_days": None,
                "paired_observations": {
                    "total": None,
                    "recorded_dates": None,
                    "valid_ic_dates": None,
                    "min": None,
                    "max": None,
                },
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
            "raw_availability": _project_raw_availability(store, result, factor=factor, dates=[], calendar_known=False),
            "common_sample": _common_sample_info(store, result),
            "financials": _financials_info(result),
        }

    calendar = sorted(
        {row["signal_date"] for row in signal_cov if isinstance(row, dict) and isinstance(row.get("signal_date"), str)}
    )
    sig_entry_by_date = {
        row["signal_date"]: row.get("entry_date")
        for row in signal_cov
        if isinstance(row, dict) and isinstance(row.get("signal_date"), str)
    }
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
    paired_obs_values: list[int] = []
    matured_dates = 0
    unmatured_dates = 0
    missing_exit_date_dates = 0
    invalid_exit_date_dates = 0
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
        raw_exit = row.get("exit_date")
        if raw_exit is None or raw_exit == "":
            missing_exit_date_dates += 1
            ic_exclusion_counts["missing_exit_date"] = ic_exclusion_counts.get("missing_exit_date", 0) + 1
            values.append(None)
            continue

        try:
            parsed_sig = _parse_date(day, "信号日期")
            parsed_exit = _parse_date(raw_exit, "退出日期")
        except ValueError:
            parsed_sig = None
            parsed_exit = None

        raw_entry = row.get("entry_date") if row.get("entry_date") is not None else sig_entry_by_date.get(day)
        parsed_entry: str | None = None
        entry_valid = True
        if raw_entry is not None and raw_entry != "":
            try:
                parsed_entry = _parse_date(raw_entry, "入场日期")
            except ValueError:
                entry_valid = False

        if (
            parsed_sig is None
            or parsed_exit is None
            or not entry_valid
            or parsed_exit < parsed_sig
            or (parsed_entry is not None and (parsed_entry < parsed_sig or parsed_exit < parsed_entry))
        ):
            invalid_exit_date_dates += 1
            ic_exclusion_counts["invalid_exit_date"] = ic_exclusion_counts.get("invalid_exit_date", 0) + 1
            values.append(None)
            continue

        if parsed_exit > eff_cutoff:
            unmatured_dates += 1
            ic_exclusion_counts["unmatured"] = ic_exclusion_counts.get("unmatured", 0) + 1
            values.append(None)
            continue

        matured_dates += 1
        val = _finite(row.get("rank_ic"))
        if val is not None:
            values.append(val)
            valid_exit_dates.append(parsed_exit)
            obs = _opt_int(row, "observations")
            if obs is not None and obs >= 0:
                paired_obs_values.append(obs)
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
        detail_parts = []
        if unmatured_dates:
            detail_parts.append(f"截至 {eff_cutoff} 未到期 {unmatured_dates} 日")
        if missing_exit_date_dates:
            detail_parts.append(f"缺少离场日 {missing_exit_date_dates} 日")
        if invalid_exit_date_dates:
            detail_parts.append(f"离场日格式或时序异常 {invalid_exit_date_dates} 日")
        if missing_daily_rows:
            detail_parts.append(f"缺诊断行 {missing_daily_rows} 日")
        if excluded_matured_dates:
            detail_parts.append(f"截面不足或常数排除 {excluded_matured_dates} 日")
        suffix = f"（{'，'.join(detail_parts)}）" if detail_parts else ""
        status_reason = f"区间内 {len(filtered_dates)} 个信号日均无有效成熟 Rank IC{suffix}。"
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

    # Label coverage summary in [eff_start, eff_end] (frozen at original evaluation end)
    label_obj = result.get("labels", {}).get(str(horizon))
    label_coverage_summary = None
    label_exclusions_list = []
    if isinstance(label_obj, dict) and isinstance(label_obj.get("coverage"), list):
        filtered_set = set(filtered_dates)
        scored_sd = 0
        valid_sd = 0
        matched_lbl_rows = 0
        lbl_complete = bool(filtered_dates)
        lbl_excl: dict[str, int] = {}
        seen_lbl_dates: set[str] = set()
        for row in label_obj["coverage"]:
            if not isinstance(row, dict) or row.get("date") not in filtered_set:
                continue
            matched_lbl_rows += 1
            seen_lbl_dates.add(row["date"])
            s_val = _opt_int(row, "scored")
            v_val = _opt_int(row, "valid_labels")
            if s_val is None or v_val is None:
                lbl_complete = False
            else:
                scored_sd += s_val
                valid_sd += v_val
            for k, c in (row.get("exclusions") or {}).items():
                lbl_excl[str(k)] = lbl_excl.get(str(k), 0) + int(c)
        if len(seen_lbl_dates) != len(filtered_set):
            lbl_complete = False
        if matched_lbl_rows > 0:
            label_coverage_summary = {
                "scope": "evaluation_end_in_signal_range",
                "evaluation_end": result.get("metadata", {}).get("end"),
                "matched_signal_dates": len(seen_lbl_dates),
                "total_signal_dates": len(filtered_dates),
                "scored_stock_days": scored_sd if lbl_complete else None,
                "valid_stock_days": valid_sd if lbl_complete else None,
                "excluded_stock_days": sum(lbl_excl.values()) if lbl_complete else None,
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

    recorded_obs_days = len(paired_obs_values)
    paired_obs_total: int | None
    if nw["n"] == 0:
        paired_obs_total = 0
    elif recorded_obs_days == nw["n"]:
        paired_obs_total = sum(paired_obs_values)
    else:
        paired_obs_total = None

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
            "invalid_exit_date_dates": invalid_exit_date_dates,
            "excluded_matured_dates": excluded_matured_dates,
            "missing_daily_rows": missing_daily_rows,
            "lag_pairs": nw["lag_pairs"],
            "paired_observations_on_valid_ic_days": paired_obs_total,
            "paired_observations": {
                "total": paired_obs_total,
                "recorded_dates": recorded_obs_days,
                "valid_ic_dates": nw["n"],
                "min": min(paired_obs_values) if paired_obs_values else None,
                "max": max(paired_obs_values) if paired_obs_values else None,
            },
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
        "financials": _financials_info(result),
    }
