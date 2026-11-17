"""Eligibility, exclusion diagnostics and ranking for one trading-day cross section."""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from finance_quant.research.factor_library import FACTOR_LIBRARY, FILLABLE_FACTORS
from finance_quant.research.gross_margin import details as gross_margin_details
from finance_quant.research.neutralization import residualize
from finance_quant.research.registry import FACTOR_DIRECTIONS


@dataclass
class SectionRanking:
    rows: list[dict]
    excluded: list[dict]
    industry: dict


def section_eligibility(section, date, specs, universe, *, dynamic, industry_history, actual_calendar, neutralization):
    day = date.strftime("%Y-%m-%d")
    names = list(specs)
    columns = ["factor_" + key for key in names]
    availability, industry_days = {}, {}
    valid = np.isfinite(section[columns].to_numpy(dtype=float))
    not_applicable = np.column_stack([_not_applicable(section, specs[name]) for name in names])
    required = np.array([specs[name].missing_policy == "exclude" for name in names])
    eligible = (valid[:, required] | not_applicable[:, required]).all(axis=1) & valid.any(axis=1)
    if dynamic:
        eligible &= section.eligible_history.to_numpy() & ~section.is_suspended.to_numpy()
    assigned = (
        industry_history.at(date, section.instrument, actual_calendar, level=neutralization.industry_level)
        if industry_history and neutralization.industry_level in industry_history.levels
        else {}
    )
    if assigned:
        known = np.array([assigned[s]["industry_status"] == "known" for s in section.instrument])
        industry_days[day] = {
            "universe": len(section),
            "known": int(known.sum()),
            "missing": sum(assigned[s]["industry_status"] == "missing" for s in section.instrument),
            "conflict": sum(assigned[s]["industry_status"] == "conflict" for s in section.instrument),
        }
        if neutralization.mode == "industry":
            eligible &= known
    availability[day] = {
        "universe": universe,
        "eligible": int(eligible.sum()),
        "factors": {
            key: {
                "valid": int(valid[:, i].sum()),
                "missing": int(universe - valid[:, i].sum() - not_applicable[:, i].sum()),
                **(
                    {
                        "positive": int(section[columns[i]].gt(0).sum()),
                        "zero": int(section[columns[i]].eq(0).sum()),
                        "negative": int(section[columns[i]].lt(0).sum()),
                        "imputed": int((eligible & ~valid[:, i] & ~not_applicable[:, i]).sum()),
                        "missing_policy": specs[key].missing_policy,
                    }
                    if specs[key].name in FILLABLE_FACTORS
                    else {}
                ),
                **(
                    {
                        "not_applicable": int(not_applicable[:, i].sum()),
                        "not_applicable_scored": int((eligible & not_applicable[:, i]).sum()),
                    }
                    if specs[key].name == "gross_margin_ttm"
                    else {}
                ),
            }
            for i, key in enumerate(names)
        },
    }
    return eligible, assigned, availability[day], industry_days.get(day)


def exclusion_rows(section, eligible, specs, *, day, assigned, neutralization, dynamic):
    diagnostics = []
    for (_, row), included in zip(section.iterrows(), eligible, strict=True):
        if included and eligible.sum() >= 2:
            continue
        reasons, raw = {}, {}
        details = _factor_details(row, specs, scored=False)
        for key, spec in specs.items():
            value = row["factor_" + key]
            raw[key] = float(value) if np.isfinite(value) else None
            if raw[key] is not None:
                continue
            reason = (
                "missing_or_stale_financial_report"
                if FACTOR_LIBRARY[spec.name].fundamental
                else "missing_or_nonpositive_"
                + ("pe_ttm" if spec.name == "value" else "pb" if spec.name == "book_to_price" else "total_mv")
                if spec.name in {"value", "book_to_price", "size"}
                else "insufficient_negative_returns_or_history"
                if spec.name == "downside_volatility"
                else "insufficient_history_or_trading_activity"
                if spec.name == "amount_anomaly"
                else "insufficient_history"
            )
            if spec.name in {"book_to_price", "size"} and bool(row.get("is_suspended", False)):
                reason = "suspended_auxiliary_value"
            if spec.name in FILLABLE_FACTORS:
                reason = details["factor_details"][key]["reason"]
            reasons[key] = reason
        if neutralization.mode == "industry" and assigned[row.instrument]["industry_status"] != "known":
            reasons["_industry"] = "industry_" + assigned[row.instrument]["industry_status"]
        if included:
            reasons["_cross_section"] = "cross_section_too_small"
        if dynamic:
            if not row.eligible_history:
                reasons["_eligibility"] = "insufficient_pre_history"
            if row.is_suspended:
                reasons["_trading"] = "suspended"
        diagnostics.append(
            {
                "date": day,
                "instrument": row.instrument,
                "raw": raw,
                "reasons": reasons,
                **details,
            }
        )
    return diagnostics


def _number(value):
    return float(value) if value is not None and np.isfinite(value) else None


def earnings_yield_details(row, spec):
    """Keep observed inputs distinct from a policy-assigned percentile score."""
    profit, cap = _number(row.get("ttm_net_profit")), _number(row.get("total_mv"))
    period, available = row.get("financial_report_period"), row.get("financial_available_at")
    missing = not np.isfinite(row["factor_" + spec.key])
    return {
        "ttm_net_profit": profit,
        "total_mv": cap,
        "report_period": str(period)[:10] if period is not None and not pd.isna(period) else None,
        "financial_available_at": str(available) if available is not None and not pd.isna(available) else None,
        "missing_policy": spec.missing_policy,
        "imputed": missing and spec.missing_policy != "exclude",
        "reason": ("missing_or_stale_ttm_profit" if profit is None else "missing_or_nonpositive_total_mv")
        if missing
        else None,
    }


def _not_applicable(section, spec):
    return (
        section.gross_margin_status.eq("not_applicable")
        if spec.name == "gross_margin_ttm"
        else pd.Series(False, index=section.index)
    )


def _fill_scores(scores, spec, section):
    if spec.name == "gross_margin_ttm":
        scores = scores.mask(_not_applicable(section, spec), 50.0)
    return scores.fillna(0.0 if spec.missing_policy == "zero" else 50.0) if spec.missing_policy != "exclude" else scores


def _scores(values, spec, section):
    scores = values.rank(pct=True, ascending=FACTOR_DIRECTIONS[spec.name] != "lower") * 100
    return _fill_scores(scores, spec, section)


def _factor_details(row, specs, *, scored):
    details = {}
    for key, spec in specs.items():
        if spec.name == "value":
            detail = earnings_yield_details(row, spec)
            detail["imputed"] = detail["imputed"] and scored
            details[key] = detail
        elif spec.name == "gross_margin_ttm":
            details[key] = gross_margin_details(row, spec, scored=scored)
    return {"factor_details": details} if details else {}


def rank_section(section, date, specs, *, assigned, neutralization):
    day = date.strftime("%Y-%m-%d")
    names = list(specs)
    industry_summary = {}
    scores = {key: _scores(section["factor_" + key], specs[key], section) for key in names}
    before_scores = scores
    before_composite = sum(scores[key] * specs[key].weight for key in names)
    before_ranks = before_composite.rank(method="min", ascending=False).astype(int)
    processed = {}
    if neutralization.mode == "industry":
        groups = section.instrument.map(lambda s: assigned[s]["industry_code"])
        processed = {
            key: residualize(
                section["factor_" + key] * (-1 if FACTOR_DIRECTIONS[specs[key].name] == "lower" else 1), groups
            )
            for key in names
        }
        if all(v.nunique() < 2 for v in processed.values()):
            excluded = [
                {
                    "date": day,
                    "instrument": row.instrument,
                    "raw": {key: _number(row["factor_" + key]) for key in names},
                    "reasons": {"_industry": "no_within_industry_variation"},
                    **_factor_details(row, specs, scored=False),
                }
                for _, row in section.iterrows()
            ]
            return SectionRanking([], excluded, {"no_residual_variation": True})
        scores = {key: _fill_scores(processed[key].rank(pct=True) * 100, specs[key], section) for key in names}
        industry_summary["singleton_stocks"] = int(groups.map(groups.value_counts()).eq(1).sum())
    composite = sum(scores[key] * specs[key].weight for key in names)
    ranks = composite.rank(method="min", ascending=False).astype(int)
    available_at = (date.tz_localize("Asia/Shanghai") + pd.Timedelta(hours=18)).isoformat()
    history = []
    for index, row in section.iterrows():
        history.append(
            {
                "instrument": row.instrument,
                "name": row["name"],
                "industry": row.industry,
                "date": day,
                "available_at": available_at,
                "raw": {key: _number(row["factor_" + key]) for key in names},
                "scores": {key: float(scores[key].loc[index]) for key in names},
                "contributions": {key: float(scores[key].loc[index] * specs[key].weight) for key in names},
                "valuation_asof": str(row.valuation_asof) if pd.notna(row.get("valuation_asof")) else day,
                "is_suspended": bool(row.get("is_suspended", False)),
                **_factor_details(row, specs, scored=True),
                "composite": float(composite.loc[index]),
                "rank": int(ranks.loc[index]),
                **(assigned[row.instrument] if assigned else {}),
                **(
                    {
                        "processed": {key: _number(processed[key].loc[index]) for key in names},
                        "before_scores": {key: float(before_scores[key].loc[index]) for key in names},
                        "before_contributions": {
                            key: float(before_scores[key].loc[index] * specs[key].weight) for key in names
                        },
                        "before_composite": float(before_composite.loc[index]),
                        "before_rank": int(before_ranks.loc[index]),
                    }
                    if neutralization.mode == "industry"
                    else {}
                ),
            }
        )
    return SectionRanking(history, [], industry_summary)
