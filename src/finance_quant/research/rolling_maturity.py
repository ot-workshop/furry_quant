"""Session-based maturity gates and common-sample Rank IC for local rolling plans."""

from __future__ import annotations

from collections import Counter
from datetime import date

import pandas as pd

from finance_quant.data.point_in_time import EXCHANGE_TZ, daily_label_maturity
from finance_quant.research.evaluation import _diagnostics, _sessions, forward_open_labels
from finance_quant.research.factor_sections import section_eligibility
from finance_quant.research.manual_formula import MANUAL_FORMULA, FormulaVersion
from finance_quant.research.market_factors import calculate_factor_values
from finance_quant.research.neutralization import NeutralizationConfig
from finance_quant.research.registry import EvaluationConfig, FactorConfig, FactorSpec

SEGMENTS = ("train", "validation", "test")
HOLDING_SESSIONS = 1
MIN_STOCKS = 3
MIN_DAYS = 2
RULES = {
    "holding_sessions": HOLDING_SESSIONS,
    "exit_maturity": "15:00 Asia/Shanghai",
    "decision_cutoff": "18:00 Asia/Shanghai before next segment",
    "embargo_sessions": 1,
    "min_stocks": MIN_STOCKS,
    "min_days": MIN_DAYS,
    "criterion": "mean_daily_common_rank_ic",
    "tie_break": "formula_id_ascending",
}
PRICE_ONLY = NeutralizationConfig(allow_latest=False)
DIAGNOSTICS = EvaluationConfig(horizons=(HOLDING_SESSIONS,), min_cross_section=MIN_STOCKS)


def aware(value: str) -> pd.Timestamp:
    if not isinstance(value, str):
        raise ValueError("Availability and report timestamps must be strings with timezone offsets")
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp) or timestamp.tzinfo is None:
        raise ValueError("Availability and report timestamps require a timezone offset")
    return timestamp.tz_convert(EXCHANGE_TZ)


def signal_day(value: str) -> str:
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("Signal days require YYYY-MM-DD")
    return value


def trading_windows(raw_folds, sessions) -> list[dict]:
    days = _sessions(sessions).strftime("%Y-%m-%d").tolist()
    index = {day: position for position, day in enumerate(days)}
    if not isinstance(raw_folds, list) or len(raw_folds) != 2:
        raise ValueError("The plan requires exactly two folds")
    folds: list[dict] = []
    for raw in raw_folds:
        if not isinstance(raw, dict) or set(raw) != set(SEGMENTS):
            raise ValueError("Each fold requires train, validation and test signal ranges")
        fold: dict = {}
        for name in SEGMENTS:
            first, last = raw[name]
            start, end = index[signal_day(first)], index[signal_day(last)]
            if start > end or (fold and start <= list(fold.values())[-1]["end_index"]):
                raise ValueError("Signal ranges must be ordered and disjoint within each fold")
            fold[name] = {"start_index": start, "end_index": end, "days": days[start : end + 1]}
        if fold["test"]["end_index"] + 1 >= len(days):
            raise ValueError("A test signal has no next-session entry")
        folds.append(fold)
    if folds[0]["test"]["end_index"] >= folds[1]["test"]["start_index"]:
        raise ValueError("The two test ranges must be ordered and disjoint")
    if any(len(folds[0][name]["days"]) != len(folds[1][name]["days"]) for name in SEGMENTS):
        raise ValueError("Corresponding segments must have equal trading-session lengths")
    return folds


def parse_evidence(document, *, data_version, market_sha256, instruments, days) -> tuple[str, dict]:
    fields = {"schema_version", "data_version", "market_sha256", "certification", "rows"}
    if not isinstance(document, dict) or set(document) != fields:
        raise ValueError("An availability evidence document must include a version, market digest and rows")
    if (
        type(document["schema_version"]) is not int
        or document["schema_version"] != 1
        or document["data_version"] != data_version
        or document["market_sha256"] != market_sha256
        or document["certification"] not in ("synthetic", "unverified")
        or not isinstance(document["rows"], list)
    ):
        raise ValueError("Availability evidence does not match the frozen market")
    seen: dict = {}
    for row in document["rows"]:
        if not isinstance(row, dict) or set(row) != {
            "instrument",
            "date",
            "open_available_at",
            "adjustment_available_at",
        }:
            raise ValueError("Invalid availability evidence row fields")
        day, instrument = signal_day(row["date"]), row["instrument"]
        if day not in days or instrument not in instruments or (day, instrument) in seen:
            raise ValueError("Availability rows must be unique and belong to the frozen pool and calendar")
        open_seen, adjustment_seen = (
            aware(row[field]) if row[field] is not None else None
            for field in ("open_available_at", "adjustment_available_at")
        )
        # Missing fields remain unknown. A date-only file cannot assert prior visibility.
        seen[day, instrument] = (
            max(open_seen, adjustment_seen) if open_seen is not None and adjustment_seen is not None else None
        )
    return document["certification"], seen


def labels_for_pool(market, sessions, instruments) -> pd.DataFrame:
    days = _sessions(sessions).strftime("%Y-%m-%d").tolist()
    observed = market.dropna(subset=["open", "adj_factor"])
    return forward_open_labels(observed, sessions, HOLDING_SESSIONS, days[-1], instruments=instruments)


def gated_labels(labels, days, evidence, cutoff, *, next_start=None, previous_day=None) -> list[dict]:
    rows = []
    for row in labels.loc[labels["date"].isin(days)].itertuples(index=False):
        entry, exit_ = row.entry_date, row.exit_date
        entry_seen = evidence.get((entry, row.instrument))
        exit_seen = evidence.get((exit_, row.instrument))
        available_at = (
            max(pd.Timestamp(daily_label_maturity(date.fromisoformat(exit_))), entry_seen, exit_seen)
            if exit_ is not None and entry_seen is not None and exit_seen is not None
            else None
        )
        if exit_ is None:
            reason = "tail_unmatured"
        elif next_start is not None and exit_ >= next_start:
            reason = "purged_cross_boundary"
        elif previous_day is not None and exit_ == previous_day:
            reason = "embargo_previous_session"
        elif row.exclusion is not None:
            reason = row.exclusion
        elif available_at is None:
            reason = "missing_availability_evidence"
        elif available_at > cutoff:
            reason = "not_available_at_cutoff"
        else:
            reason = None
        rows.append(
            {
                "date": row.date,
                "instrument": row.instrument,
                "entry_date": entry,
                "exit_date": exit_,
                "label_available_at": available_at.isoformat() if available_at is not None else None,
                "forward_return": None if reason else float(row.forward_return),
                "label_exclusion": reason,
            }
        )
    return rows


def formula_config(version: FormulaVersion) -> FactorConfig:
    return FactorConfig((FactorSpec(MANUAL_FORMULA, 1, formula_version=version),))


def causal_scores(market, sessions, instruments, through, version) -> dict:
    days = pd.DatetimeIndex(sessions)
    days = days[days <= pd.Timestamp(through)]
    keys = pd.MultiIndex.from_product([sorted(instruments), days], names=["instrument", "date"])
    # Empty source sessions remain NaN on the fixed calendar and never shorten a lookback.
    quotes = market.loc[market["date"].le(pd.Timestamp(through))]
    quotes = quotes.set_index(["instrument", "date"]).reindex(keys).reset_index()
    config = formula_config(version)
    frame, calendar, _ = calculate_factor_values(quotes, config, exchange_calendar=days)
    specs = {MANUAL_FORMULA: config.specs[0]}
    scores = {}
    for day, section in frame.groupby("date", sort=True):
        eligible, *_ = section_eligibility(
            section,
            day,
            specs,
            len(instruments),
            dynamic=False,
            industry_history=None,
            actual_calendar=calendar,
            neutralization=PRICE_ONLY,
        )
        for row in section.loc[eligible].itertuples(index=False):
            scores[day.strftime("%Y-%m-%d"), row.instrument] = float(row.factor_manual_formula)
    return scores


def with_scores(rows, scores_by_version) -> list[dict]:
    enriched = []
    for row in rows:
        predictions = {key: scores.get((row["date"], row["instrument"])) for key, scores in scores_by_version.items()}
        enriched.append(
            {
                **row,
                "scores": predictions,
                "exclusion": row["label_exclusion"]
                or ("missing_candidate_score" if None in predictions.values() else None),
            }
        )
    return enriched


def diagnostics(rows, formula_id) -> dict:
    section = pd.DataFrame(
        [{**row, "prediction": row["scores"][formula_id] if row["exclusion"] is None else None} for row in rows]
    )
    section["forward_return"] = pd.to_numeric(section["forward_return"])
    section["prediction"] = pd.to_numeric(section["prediction"])
    section.loc[section["exclusion"].notna(), "forward_return"] = float("nan")
    return _diagnostics(section, DIAGNOSTICS)


def row_summary(rows) -> dict:
    counts = Counter(row["exclusion"] for row in rows)
    return {"rows": len(rows), "retained": counts.pop(None, 0), "exclusions": dict(sorted(counts.items()))}


def choose(rows, formula_ids) -> dict:
    by_formula = {formula: diagnostics(rows, formula)["daily"] for formula in formula_ids}
    common = set.intersection(
        *({row["date"] for row in data if row["rank_ic"] is not None} for data in by_formula.values())
    )
    means = {
        formula: sum(row["rank_ic"] for row in data if row["date"] in common) / len(common)
        for formula, data in by_formula.items()
        if common
    }
    winner = min(formula_ids, key=lambda formula: (-means[formula], formula)) if len(common) >= MIN_DAYS else None
    return {
        "winner": winner,
        "reason": None if winner else "insufficient_common_rank_ic_dates",
        "valid_dates": sorted(common),
        "mean_rank_ic": {formula: means.get(formula) for formula in formula_ids},
        "daily": by_formula,
    }
