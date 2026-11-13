"""Eligibility, exclusion diagnostics and ranking for one trading-day cross section."""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from finance_quant.research.factor_library import FACTOR_LIBRARY
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
    eligible = valid.all(axis=1)
    if dynamic:
        eligible &= section.eligible_history.to_numpy() & ~section.is_suspended.to_numpy()
    assigned = (
        industry_history.at(date, section.instrument, actual_calendar, level=neutralization.industry_level)
        if industry_history
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
            key: {"valid": int(valid[:, i].sum()), "missing": int(universe - valid[:, i].sum())}
            for i, key in enumerate(names)
        },
    }
    return eligible, assigned, availability[day], industry_days.get(day)


def exclusion_rows(section, eligible, specs, *, day, assigned, neutralization, dynamic):
    diagnostics = []
    for (_, row), included in zip(section.iterrows(), eligible):
        if included and eligible.sum() >= 2:
            continue
        reasons, raw = {}, {}
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
        diagnostics.append({"date": day, "instrument": row.instrument, "raw": raw, "reasons": reasons})
    return diagnostics


def rank_section(section, date, specs, *, assigned, neutralization):
    day = date.strftime("%Y-%m-%d")
    names = list(specs)
    industry_summary = {}
    scores = {
        key: section["factor_" + key].rank(pct=True, ascending=FACTOR_DIRECTIONS[specs[key].name] != "lower") * 100
        for key in names
    }
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
                    "raw": {key: float(row["factor_" + key]) for key in names},
                    "reasons": {"_industry": "no_within_industry_variation"},
                }
                for _, row in section.iterrows()
            ]
            return SectionRanking([], excluded, {"no_residual_variation": True})
        scores = {key: processed[key].rank(pct=True) * 100 for key in names}
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
                "raw": {key: float(row["factor_" + key]) for key in names},
                "scores": {key: float(scores[key].loc[index]) for key in names},
                "contributions": {key: float(scores[key].loc[index] * specs[key].weight) for key in names},
                "valuation_asof": str(row.valuation_asof) if pd.notna(row.get("valuation_asof")) else day,
                "is_suspended": bool(row.get("is_suspended", False)),
                "composite": float(composite.loc[index]),
                "rank": int(ranks.loc[index]),
                **(assigned[row.instrument] if assigned else {}),
                **(
                    {
                        "processed": {key: float(processed[key].loc[index]) for key in names},
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
