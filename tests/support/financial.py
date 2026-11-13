"""Synthetic financial statements for point-in-time tests.

Each ``report`` is one row of one statement API response. Tests build observations,
publish versions and assert what a research run could have known on a given day.
"""

from __future__ import annotations

import pandas as pd

from finance_quant.data.financials import FinancialStore, merge_observations
from finance_quant.data.providers import FINANCIAL_VALUE_FIELDS, normalize_financial_statement

TRADING_DAYS = pd.bdate_range("2024-01-02", "2027-06-30")


def raw_rows(**values) -> pd.DataFrame:
    """A raw Tushare-shaped frame with the given overrides."""
    row = {field: None for field in FINANCIAL_VALUE_FIELDS}
    row.update({key: str(value) if key.endswith("date") else value for key, value in values.items()})
    return pd.DataFrame([row])


def statement_frame(ts_code: str, statement: str, report_type: int, **values) -> pd.DataFrame:
    return normalize_financial_statement(raw_rows(ts_code=ts_code, **values), statement, report_type, ts_code)


def income(ts_code="600000.SH", report_type=1, period="20241231", announce="20250320", update_flag=None, **values):
    payload = {
        "total_revenue": 100.0,
        "oper_cost": 60.0,
        "n_income_attr_p": 10.0,
        **values,
        "end_date": period,
        "ann_date": announce,
        "f_ann_date": announce,
        **({"update_flag": update_flag} if update_flag is not None else {}),
    }
    return statement_frame(ts_code, "income", report_type, **payload)


def balance(ts_code="600000.SH", report_type=1, period="20241231", announce="20250320", **values):
    payload = {
        "total_assets": 500.0,
        "total_liab": 200.0,
        "total_hldr_eqy_exc_min_int": 300.0,
        **values,
        "end_date": period,
        "ann_date": announce,
        "f_ann_date": announce,
    }
    return statement_frame(ts_code, "balance", report_type, **payload)


def cashflow(ts_code="600000.SH", report_type=1, period="20241231", announce="20250320", **values):
    payload = {
        "n_cashflow_act": 8.0,
        **values,
        "end_date": period,
        "ann_date": announce,
        "f_ann_date": announce,
    }
    return statement_frame(ts_code, "cashflow", report_type, **payload)


def publish(root, parts, *, calendar=TRADING_DAYS, seen_on="2025-06-30") -> dict:
    """Merge statement frames per stock into observations and publish one version."""
    store = FinancialStore(root)
    store.save_calendar(pd.DataFrame({"date": calendar, "is_open": True}))
    grouped: dict[str, pd.DataFrame] = {}
    for frame in parts:
        for code, part in frame.groupby("ts_code"):
            grouped[code] = merge_observations(grouped.get(code), part, seen_on=seen_on)
    for code, observations in grouped.items():
        store.save_stock(code, observations, fetched_at="2025-06-30T00:00:00+00:00")
    return store.publish()
