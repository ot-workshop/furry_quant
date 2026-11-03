"""Independently reconcile live web tasks against their frozen market data.

Run after frontend's explicit test:live command; no network or API credentials.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

from finance_quant.application.research_requests import snapshot_registry
from finance_quant.configuration import load_config
from finance_quant.storage import ResultStore


def check(evidence: dict, data_root: Path, runs_root: Path, *, compare_variants=True) -> dict:
    assert evidence.get('status') == 'passed', 'Complete the live form checks before auditing artifacts'
    registry = snapshot_registry(data_root)
    store = ResultStore(runs_root)
    cases, checked_factors, histories = {}, set(), {}
    raw_checks = score_checks = account_days = label_checks = expected_failures = trade_checks = 0
    unique_codes, halt_rows = set(), set()
    for case in evidence["cases"]:
        if case['task']['status'] != 'succeeded':
            assert not compare_variants and case['task']['status'] == 'failed'
            assert case['task']['result'] is None and case['task']['error_details']['issues']
            expected_failures += 1
            continue
        request, result = case["request"], case["task"]["result"]
        codes = set(request["data"]["codes"])
        unique_codes.update(codes)
        market, _, calendar, manifest = registry[result["data_version"]]
        assert set(manifest["codes"]) == codes
        factor = store.get("factors", result["factor_run_id"])
        backtest = store.get("backtests", result["backtest_run_id"])
        evaluation = store.get("evaluations", result["evaluation_run_id"])
        expected_config = sorted(request["factors"], key=lambda item: item["name"])
        assert factor["metadata"]["factor_config"] == expected_config
        assert backtest["metadata"]["factor_run_id"] == evaluation["metadata"]["factor_run_id"] == result["factor_run_id"]
        assert backtest["metadata"]["config"]["factor_run_id"] == result["factor_run_id"]
        assert backtest["metadata"]["data_version"] == evaluation["metadata"]["data_version"] == manifest["id"]
        for key, value in request["backtest"].items():
            assert backtest["metadata"]["provenance"][key] == value
        assert evaluation["metadata"]["start"] == request["backtest"]["start"]
        assert evaluation["metadata"]["end"] == request["backtest"]["end"]
        assert {row["instrument"] for row in factor["history"]} == {code[-2:] + code[:6] for code in codes}
        histories[case["key"]] = {(row["date"], row["instrument"]): row for row in factor["history"]}
        if result["factor_run_id"] not in checked_factors:
            checked_factors.add(result["factor_run_id"])
            # Recalculate from raw frozen quotes, independently of the factor workflow.
            frame = market.sort_values(["instrument", "date"]).copy()
            adjusted = frame["close"] * frame["adj_factor"]
            for spec in expected_config:
                if spec["name"] == "momentum":
                    frame["momentum"] = adjusted.groupby(frame.instrument).pct_change(spec["window"], fill_method=None)
                elif spec["name"] == "volatility":
                    returns = adjusted.groupby(frame.instrument).pct_change(fill_method=None)
                    frame["volatility"] = returns.groupby(frame.instrument).transform(lambda values: values.rolling(spec["window"], min_periods=spec["window"]).std()) * np.sqrt(252)
                else:
                    frame["value"] = 1 / frame.pe_ttm.where(frame.pe_ttm > 0)
            names = [spec["name"] for spec in expected_config]
            predictions = histories[case["key"]]
            for date, section in frame.groupby("date"):
                eligible = section.loc[np.isfinite(section[names].to_numpy()).all(axis=1)]
                if len(eligible) < 2:
                    continue
                scores = {name: eligible[name].rank(pct=True, ascending=name != "volatility") * 100 for name in names}
                for index, row in eligible.iterrows():
                    saved = predictions[(date.strftime("%Y-%m-%d"), row.instrument)]
                    for name in names:
                        assert math.isclose(saved["raw"][name], row[name], abs_tol=1e-10)
                        assert math.isclose(saved["scores"][name], scores[name].loc[index], abs_tol=1e-10)
                        raw_checks += 1
                        score_checks += 1
                    composite = sum(scores[spec["name"]].loc[index] * spec["weight"] for spec in expected_config)
                    assert math.isclose(saved["composite"], composite, abs_tol=1e-9)
                    score_checks += 1
        positions = defaultdict(list)
        for row in backtest["positions"]:
            positions[row["date"]].append(row)
        for row in backtest["equity"][1:]:
            value = row["cash"] + sum(p["price"] * p["amount"] for p in positions[row["date"]])
            assert math.isclose(value, row["account_value"], abs_tol=1e-5)
            assert math.isclose(value / request["backtest"]["initial_cash"], row["equity"], abs_tol=1e-10)
            account_days += 1
        assert math.isclose(sum(row["cost"] for row in backtest["trades"]), backtest["summary"]["metrics"]["total_cost_amount"], abs_tol=1e-6)
        quotes = market.set_index(["instrument", "date"])
        by_day = defaultdict(list)
        for trade in backtest['trades']:
            quote = quotes.loc[(trade['instrument'], pd.Timestamp(trade['date']))]
            assert math.isclose(trade['price'], quote.open, rel_tol=2e-6, abs_tol=2e-5)
            assert math.isclose(trade['price'] * trade['amount'], trade['value'], abs_tol=1e-5)
            rate = request['backtest']['open_cost' if trade['side'] == 'buy' else 'close_cost']
            assert math.isclose(trade['cost'], max(request['backtest']['min_cost'], trade['value'] * rate), abs_tol=1e-6)
            if trade['side'] == 'buy':
                assert math.isclose(trade['amount'] / 100, round(trade['amount'] / 100), abs_tol=1e-5)
            by_day[trade['date']].append(trade)
            trade_checks += 1
        prior_cash = request['backtest']['initial_cash']
        for row in backtest['equity'][1:]:
            expected_cash = prior_cash + sum((t['value'] if t['side'] == 'sell' else -t['value']) - t['cost'] for t in by_day[row['date']])
            assert math.isclose(row['cash'], expected_cash, abs_tol=1e-5)
            for position in positions[row['date']]:
                close = quotes.loc[(position['instrument'], pd.Timestamp(row['date']))].close
                assert math.isclose(position['price'], close, rel_tol=2e-6, abs_tol=2e-5)
            prior_cash = row['cash']
        for row in market.loc[market.get('is_suspended', pd.Series(False, index=market.index))].itertuples():
            halt_rows.add((manifest['id'], row.instrument, row.date.strftime('%Y-%m-%d')))
            assert not any(t['date'] == row.date.strftime('%Y-%m-%d') and t['instrument'] == row.instrument for t in backtest['trades'])
        sessions = pd.DatetimeIndex(calendar.loc[calendar.is_open, "date"])
        for horizon, label in evaluation["labels"].items():
            for row in label["observations"]:
                assert request["backtest"]["start"] <= row["entry_date"] <= row["exit_date"] <= request["backtest"]["end"]
                signal, entry, exit_ = map(pd.Timestamp, (row["date"], row["entry_date"], row["exit_date"]))
                assert sessions.get_loc(entry) == sessions.get_loc(signal) + 1
                assert sessions.get_loc(exit_) == sessions.get_loc(entry) + int(horizon)
                opening, closing = quotes.loc[(row["instrument"], entry)], quotes.loc[(row["instrument"], exit_)]
                assert not opening.get('is_suspended', False) and not closing.get('is_suspended', False)
                expected = closing.open * closing.adj_factor / (opening.open * opening.adj_factor) - 1
                assert math.isclose(row["forward_return"], expected, abs_tol=1e-10)
                label_checks += 1
        cases[case["key"]] = result

    if not compare_variants:
        return {'status': 'passed', 'successful_cases': len(cases), 'expected_failures': expected_failures,
                'unique_stocks': len(unique_codes), 'raw_factor_checks': raw_checks, 'score_checks': score_checks,
                'account_days': account_days, 'trade_checks': trade_checks,
                'forward_label_checks': label_checks, 'halt_rows_checked': len(halt_rows)}

    baseline, weighted, windowed, subset = (histories[key] for key in ("baseline", "weights", "window", "pool"))
    common = baseline.keys() & weighted.keys()
    assert all(baseline[k]["raw"] == weighted[k]["raw"] for k in common)
    changed_weights = sum(not math.isclose(baseline[k]["composite"], weighted[k]["composite"], abs_tol=1e-9) for k in common)
    changed_windows = sum(not math.isclose(baseline[k]["raw"]["momentum"], windowed[k]["raw"]["momentum"], abs_tol=1e-10) for k in baseline.keys() & windowed.keys())
    changed_pool = sum(baseline[k]["scores"] != subset[k]["scores"] for k in baseline.keys() & subset.keys())
    assert changed_weights > 0 and changed_windows > 0 and changed_pool > 0
    assert cases["baseline"]["factor_run_id"] == cases["end-date"]["factor_run_id"]
    assert cases["baseline"]["backtest_run_id"] != cases["end-date"]["backtest_run_id"]
    assert cases["baseline"]["evaluation_run_id"] != cases["end-date"]["evaluation_run_id"]
    return {"status": "passed", "cases": len(cases), "raw_factor_checks": raw_checks, "score_checks": score_checks,
            "account_days": account_days, "forward_label_checks": label_checks,
            "changed_weight_composites": changed_weights, "changed_window_values": changed_windows, "changed_pool_percentiles": changed_pool}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument('--runs-dir', type=Path, help='Use artifacts from an isolated QA queue')
    parser.add_argument('--independent-pools', action='store_true', help='Audit varied pools without the fixed five-case comparison')
    args = parser.parse_args()
    config = load_config(args.config)
    result = check(json.loads(args.evidence.read_text(encoding="utf-8")), config.data.root, args.runs_dir or config.runs_dir,
                   compare_variants=not args.independent_pools)
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
