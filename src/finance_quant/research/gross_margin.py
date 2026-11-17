"""Gross margin on matching visible income statements, with explicit applicability."""

import math

import numpy as np
import pandas as pd

CONTEXT = "gross_margin_context"
FINANCIAL_TYPES = {"2", "3", "4", "7"}
FINANCIAL_INDUSTRIES = {"480000", "490000"}
REASONS = {
    "financial_statement": "专门金融报表不适用通用毛利率",
    "historical_financial_industry": "当时历史行业为银行或非银金融，通用毛利率不适用",
    "unknown_statement_type": "当时报表类型未确认",
    "no_report": "当时没有可用利润表",
    "stale_report": "当时可用利润表已过期",
    "missing_ttm_history": "缺少计算TTM所需的利润表",
    "mixed_statement_types": "TTM所需报表类型不一致或未确认",
    "missing_revenue": "营业收入字段缺失",
    "missing_operating_cost": "营业成本字段缺失",
    "nonpositive_revenue": "TTM营业收入非正值",
}


def _number(value):
    return float(value) if value is not None and math.isfinite(value) else None


def report_context(reports, period):
    """Every entry of reports is already visible at the event's timestamp."""
    pieces = (
        [(period, 1)]
        if period.month == 12
        else [(period, 1), (pd.Timestamp(period.year - 1, 12, 31), 1), (period - pd.DateOffset(years=1), -1)]
    )
    current = reports[period]
    company_type = current.get("comp_type")
    company_type = str(company_type) if pd.notna(company_type) else None
    sources = []
    for date, sign in pieces:
        row = reports.get(date)
        sources.append(
            {
                "report_period": date.strftime("%Y-%m-%d"),
                "coefficient": sign,
                "available_at": row["available_at"].isoformat()
                if row and row.get("available_at") is not None
                else None,
                "company_type": str(row["comp_type"]) if row and pd.notna(row.get("comp_type")) else None,
                "revenue": _number(row.get("revenue")) if row else None,
                "operating_cost": _number(row.get("oper_cost")) if row else None,
            }
        )

    def ttm(field):
        values = [source[field] for source in sources]
        return (
            sum(value * source["coefficient"] for value, source in zip(values, sources, strict=True))
            if all(value is not None for value in values)
            else None
        )

    revenue, cost = ttm("revenue"), ttm("operating_cost")
    context = {
        "status": "missing",
        "reason": None,
        "company_type": company_type,
        "revenue_ttm": revenue,
        "operating_cost_ttm": cost,
        "revenue_field": "revenue",
        "classification_source": "visible_income_statement",
        "components": sources,
        "raw": None,
    }
    if company_type in FINANCIAL_TYPES:
        context.update(status="not_applicable", reason="financial_statement")
    elif company_type != "1":
        context["reason"] = "unknown_statement_type"
    elif any(date not in reports for date, _ in pieces):
        context["reason"] = "missing_ttm_history"
    elif any(source["company_type"] != "1" for source in sources):
        context["reason"] = "mixed_statement_types"
    elif revenue is None:
        context["reason"] = "missing_revenue"
    elif cost is None:
        context["reason"] = "missing_operating_cost"
    elif revenue <= 0:
        context["reason"] = "nonpositive_revenue"
    else:
        context.update(status="applicable", raw=(revenue - cost) / revenue)
    return context


def classify_frame(frame, industry_history, calendar):
    """Use report types first; only pinned historical financial industries can resolve unknowns."""
    frame = frame.reset_index(drop=True).copy()
    contexts = [
        dict(value)
        if isinstance(value, dict)
        else {
            "status": "missing",
            "reason": "no_report",
            "company_type": None,
            "revenue_ttm": None,
            "operating_cost_ttm": None,
            "revenue_field": "revenue",
            "classification_source": "unknown",
            "components": [],
            "raw": None,
        }
        for value in frame[CONTEXT]
    ]
    if industry_history is not None and "L1" in industry_history.levels:
        for date, section in frame.groupby("date", sort=False):
            unknown = [
                index for index in section.index if contexts[index]["reason"] in {"no_report", "unknown_statement_type"}
            ]
            if not unknown:
                continue
            assigned = industry_history.at(date, frame.loc[unknown, "instrument"], calendar, level="L1")
            for index in unknown:
                evidence = assigned[frame.loc[index, "instrument"]]
                if evidence["industry_status"] == "known":
                    contexts[index]["historical_industry"] = {
                        key: evidence.get(key) for key in ("industry_code", "industry_name", "classification")
                    }
                    if (
                        evidence.get("classification") in {"SW2014", "SW2021"}
                        and evidence["industry_code"] in FINANCIAL_INDUSTRIES
                    ):
                        contexts[index].update(
                            status="not_applicable",
                            reason="historical_financial_industry",
                            classification_source="signal_date_historical_industry",
                        )
    frame[CONTEXT] = contexts
    frame["gross_margin_status"] = [context["status"] for context in contexts]
    frame["gross_margin_ttm"] = [context["raw"] if context["raw"] is not None else np.nan for context in contexts]
    return frame


def details(row, spec, *, scored):
    context = dict(row[CONTEXT])
    status = context["status"]
    assigned = status == "not_applicable" or (status == "missing" and spec.missing_policy != "exclude")
    return {
        **context,
        "report_period": str(row.financial_report_period)[:10] if pd.notna(row.financial_report_period) else None,
        "financial_available_at": str(row.financial_available_at) if pd.notna(row.financial_available_at) else None,
        "missing_policy": spec.missing_policy,
        "not_applicable_score": 50,
        "imputed": bool(scored and assigned),
        "reason_label": REASONS.get(context["reason"]),
    }
