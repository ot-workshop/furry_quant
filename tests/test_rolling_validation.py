import json
from datetime import date

import pandas as pd
import pytest
from test_industry_neutralization import history
from test_manual_formula import CODES, A, B, C
from test_real_workflow import sample_market

from finance_quant.application import rolling_validation
from finance_quant.checksums import file_sha256
from finance_quant.data.industries import publish_industries
from finance_quant.data.managed import atomic_json
from finance_quant.data.market import write_market_snapshot_files
from finance_quant.data.point_in_time import daily_label_maturity
from finance_quant.manual_formula import main
from finance_quant.research.rolling_maturity import (
    aware,
    choose,
    gated_labels,
    labels_for_pool,
    parse_evidence,
    with_scores,
)
from finance_quant.storage import ResultStore


def test_label_uses_holiday_sessions_and_keeps_missing_exit():
    sessions = pd.DatetimeIndex(["2025-01-24", "2025-01-27", "2025-02-05"])
    frame = pd.DataFrame(
        [
            {"date": day, "instrument": instrument, "open": price, "adj_factor": 1.0, "is_suspended": halted}
            for instrument, data in {
                A: [(sessions[0], 10, False), (sessions[1], 10, False), (sessions[2], 11, False)],
                B: [(sessions[0], 10, False), (sessions[1], 10, False), (sessions[2], None, False)],
                C: [(sessions[1], 10, True), (sessions[2], 11, False)],
            }.items()
            for day, price, halted in data
        ]
    )
    labels = labels_for_pool(frame, sessions, (A, B, C))
    evidence = {
        (day, stock): aware(f"{day}T12:00:00+08:00") for day in ("2025-01-27", "2025-02-05") for stock in (A, B, C)
    }
    rows = {
        row["instrument"]: row
        for row in gated_labels(labels, ["2025-01-24"], evidence, aware("2025-02-05T15:00:00+08:00"))
    }
    assert rows[A]["entry_date"] == "2025-01-27" and rows[A]["exit_date"] == rows[B]["exit_date"] == "2025-02-05"
    assert rows[A]["forward_return"] == pytest.approx(0.1)
    assert rows[A]["label_available_at"] == daily_label_maturity(date(2025, 2, 5)).isoformat()
    assert rows[B]["label_exclusion"] == "missing_entry_or_exit" and rows[B]["forward_return"] is None
    assert rows[C]["label_exclusion"] == "non_trading_entry_or_exit"
    absent = labels_for_pool(frame.loc[frame["instrument"].ne(B)], sessions, (A, B, C))
    missing = absent.loc[absent["date"].eq("2025-01-24") & absent["instrument"].eq(B)].iloc[0]
    assert missing["exclusion"] == "missing_entry_or_exit" and missing["exit_date"] == "2025-02-05"


def test_exact_decision_cutoff_purge_embargo_and_insufficient_common_dates():
    sessions = pd.bdate_range("2025-01-06", periods=8)
    days = sessions.strftime("%Y-%m-%d").tolist()
    frame = pd.DataFrame(
        [
            {"date": day, "instrument": stock, "open": 10 + i * move, "adj_factor": 1.0}
            for i, day in enumerate(sessions)
            for stock, move in ((A, 0.1), (B, 0), (C, -0.1))
        ]
    )
    labels = labels_for_pool(frame, sessions, (A, B, C))
    cutoff = aware(f"{days[6]}T18:00:00+08:00")
    evidence = {
        (days[4], A): aware(f"{days[4]}T15:00:00+08:00"),
        (days[4], B): aware(f"{days[4]}T15:00:00+08:00"),
        (days[5], A): cutoff,
        (days[5], B): cutoff + pd.Timedelta(seconds=1),
    }
    rows = gated_labels(labels, days[3:6], evidence, cutoff, next_start=days[7], previous_day=days[6])
    by_key = {(row["date"], row["instrument"]): row for row in rows}
    assert by_key[days[3], A]["label_exclusion"] is None
    assert by_key[days[3], B]["label_exclusion"] == "not_available_at_cutoff"
    assert by_key[days[3], C]["label_exclusion"] == "missing_availability_evidence"
    assert {row["label_exclusion"] for row in rows if row["date"] == days[4]} == {"embargo_previous_session"}
    assert {row["label_exclusion"] for row in rows if row["date"] == days[5]} == {"purged_cross_boundary"}
    observed = with_scores(rows, {"a": {(days[3], A): 2.0}, "b": {(days[3], A): 3.0}})
    assert choose(observed, ("a", "b"))["reason"] == "insufficient_common_rank_ic_dates"


def test_evidence_requires_both_timestamped_inputs_and_the_frozen_market_digest():
    day = "2025-02-05"
    row = {
        "date": day,
        "instrument": A,
        "open_available_at": f"{day}T09:30:00+08:00",
        "adjustment_available_at": f"{day}T10:00:01Z",
    }
    document = {
        "schema_version": 1,
        "data_version": "frozen",
        "market_sha256": "a" * 64,
        "certification": "unverified",
        "rows": [row, {**row, "instrument": B, "adjustment_available_at": None}],
    }
    options = {"data_version": "frozen", "market_sha256": "a" * 64, "instruments": {A, B}, "days": {day}}
    certification, rows = parse_evidence(document, **options)
    assert certification == "unverified"
    assert rows[day, A].isoformat() == f"{day}T18:00:01+08:00"
    assert rows[day, B] is None
    with pytest.raises(ValueError, match="frozen market"):
        parse_evidence({**document, "market_sha256": "b" * 64}, **options)


def test_rank_ic_uses_only_dates_valid_for_both_and_breaks_ties_by_id():
    rows = []
    for day in ("2025-01-07", "2025-01-08", "2025-01-09"):
        for instrument, value in zip((A, B, C), (-0.1, 0, 0.1), strict=True):
            rows.append(
                {
                    "date": day,
                    "entry_date": "2025-01-10",
                    "exit_date": "2025-01-13",
                    "instrument": instrument,
                    "forward_return": value,
                    "exclusion": None,
                    "scores": {"a": value, "b": 1.0 if day == "2025-01-09" else value},
                }
            )
    result = choose(rows, ("b", "a"))
    assert result["winner"] == "a" and result["valid_dates"] == ["2025-01-07", "2025-01-08"]
    assert result["mean_rank_ic"] == {"a": 1.0, "b": 1.0}


def _market_fixture(batch, days, version, *, future=False, validation=False):
    instruments = (A, B, C)
    template, benchmark_template = sample_market()
    market = (
        template.drop_duplicates("instrument").drop(columns="date").merge(pd.DataFrame({"date": days}), how="cross")
    )
    for field in ("open", "close"):
        market[field] = 10.0
    for j in (7, 19):
        closes = {
            j - 3: (10.4, 10, 9.6),
            j - 2: (9.6, 10, 10.4),
            j - 1: (10.4, 10, 9.6),
            j + 3: (9.6, 10, 10.4),
            j + 4: (9.6, 10, 10.4),
        }
        opens = {j + 2: (10.2, 10, 9.8), j + 3: (10.30, 10.25, 9.81), j + 7: (10.2, 10, 9.8), j + 8: (10.4, 10, 9.6)}
        if j == 19 and future:
            opens.update({j + 7: (9.8, 10, 10.2), j + 8: (9.6, 10, 10.4)})
        if j == 19 and validation:
            opens[j + 1] = (10.5, 10, 10)
        for field, values in (("close", closes), ("open", opens)):
            for index, prices in values.items():
                for instrument, price in zip(instruments, prices, strict=True):
                    market.loc[market["date"].eq(days[index]) & market["instrument"].eq(instrument), field] = price
    market["high"] = market[["open", "close"]].max(axis=1) * 1.01
    market["low"] = market[["open", "close"]].min(axis=1) * 0.99
    benchmark = pd.concat([benchmark_template.iloc[[0]]] * len(days), ignore_index=True).assign(date=days)
    leaf = batch / "snapshots" / version
    leaf.mkdir(parents=True)
    files = write_market_snapshot_files(leaf, market, benchmark, pd.DataFrame({"date": days, "is_open": True}))
    manifest = {
        "schema_version": "market-v2",
        "id": version,
        "snapshot_path": f"snapshots/{version}",
        "universe_type": "fixed_user_selection",
        "start": days[0].strftime("%Y%m%d"),
        "end": days[-1].strftime("%Y%m%d"),
        "codes": list(CODES),
        "benchmark": "000300.SH",
        "files": files,
    }
    atomic_json(leaf / "manifest.json", manifest)
    atomic_json(batch / "manifest.json", manifest)
    return leaf, manifest


def test_rolling_cli_seals_selection_and_replays_original_inputs(tmp_path, capsys, monkeypatch):
    store = ResultStore(tmp_path / "runs")
    data = tmp_path / "data"
    batch = data / "research" / "downloads" / "rolling"
    days = pd.bdate_range("2025-01-06", periods=30)
    ds = days.strftime("%Y-%m-%d").tolist()
    formula = tmp_path / "formula.json"
    plan_path = tmp_path / "plan.json"

    def command(*args, error=None):
        capsys.readouterr()
        status = main(["--runs-dir", str(store.root), *args])
        captured = capsys.readouterr()
        if error is not None:
            assert status == 1 and error in captured.err and not captured.out
            return None
        assert status == 0, captured.err
        return json.loads(captured.out)

    def register(window):
        atomic_json(formula, {"operator": "price_return", "window": window})
        return command("register", str(formula))["formula_id"]

    v1, v2 = register(2), register(3)

    def plan(version, *, future=False, validation=False, no_evidence=False):
        leaf, manifest = _market_fixture(batch, days, version, future=future, validation=validation)
        evidence_path = data / f"{version}.json"
        atomic_json(
            evidence_path,
            {
                "schema_version": 1,
                "data_version": version,
                "market_sha256": manifest["files"]["market.parquet"],
                "certification": "synthetic",
                "rows": []
                if no_evidence
                else [
                    {
                        "date": day,
                        "instrument": stock,
                        "open_available_at": f"{day}T09:30:00+08:00",
                        "adjustment_available_at": f"{day}T15:00:00+08:00",
                    }
                    for day in ds
                    for stock in (A, B, C)
                ],
            },
        )
        atomic_json(
            plan_path,
            {
                "research": {
                    "data": {"root": str(batch), "start": ds[0], "end": ds[-1], "codes": list(CODES)},
                    "backtest": {"start": ds[13], "end": ds[26], "topk": 1},
                },
                "formula_ids": [v2, v1],
                "folds": [
                    {"train": [ds[j - 4], ds[j - 1]], "validation": [ds[j], ds[j + 4]], "test": [ds[j + 5], ds[j + 6]]}
                    for j in (7, 19)
                ],
                "report_cutoff": f"{ds[-1]}T18:00:00+08:00",
                "evidence": evidence_path.name,
            },
        )
        identifier = command("rolling-plan", "--data-root", str(data), "--plan", str(plan_path))["rolling_id"]
        return identifier, leaf, evidence_path

    original, leaf, evidence = plan("original")
    assert store.read_experiment_record(original, "plan")["point_in_time_certified"] is False
    assert not store.exists("experiments", original)
    calls = []
    run_configured = rolling_validation.run_configured
    expected_id = original

    def observed(config, *, result_store, frozen_snapshot):
        chosen = result_store.read_experiment_record(expected_id, "selection")
        position = len(calls) % 2
        formula_id = config.factors.specs[0].formula_version.id
        assert chosen["folds"][position]["choice"]["winner"] == formula_id
        calls.append(formula_id)
        return run_configured(config, result_store=result_store, frozen_snapshot=frozen_snapshot)

    monkeypatch.setattr(rolling_validation, "run_configured", observed)
    run = lambda identifier: command("rolling-run", "--data-root", str(data), "--rolling-id", identifier)
    first = run(original)
    choice = store.read_experiment_record(original, "selection")
    assert calls == [v1, v1]
    assert choice["folds"][1]["choice"]["valid_dates"] == ds[19:21]
    assert run(original) == first and len(calls) == 2

    future, _, _ = plan("future", future=True)
    expected_id = future
    second = run(future)
    changed_future = store.read_experiment_record(future, "selection")
    assert changed_future == choice and calls[-2:] == [v1, v1]
    old_test = store.get("evaluations", first["folds"][1]["evaluation_id"])
    new_test = store.get("evaluations", second["folds"][1]["evaluation_id"])
    assert old_test["rank_ic"]["summary"]["rank_ic"]["mean"] == pytest.approx(1)
    assert new_test["rank_ic"]["summary"]["rank_ic"]["mean"] == pytest.approx(-1)

    changed, _, _ = plan("validation", validation=True)
    expected_id = changed
    assert [fold["winner"] for fold in run(changed)["folds"]] == [v1, v2]
    assert calls[-2:] == [v1, v2]

    insufficient, _, _ = plan("insufficient", no_evidence=True)
    expected_id = insufficient
    count = len(calls)
    assert run(insufficient)["folds"] == [{"winner": None, "reason": "insufficient_common_rank_ic_dates"}] * 2
    assert len(calls) == count
    assert all(
        fold["validation"]["summary"]["exclusions"]["missing_availability_evidence"] > 0
        for fold in store.read_experiment_record(insufficient, "selection")["folds"]
    )

    register(4)
    publish_industries(history([A, B, C]).frame)
    paths = [
        store.path("experiments", original),
        store.path("experiments", original).with_name("plan.json"),
        store.path("experiments", original).with_name("selection.json"),
    ]
    for fold in first["folds"]:
        paths += [
            store.path(category, fold[key])
            for category, key in (
                ("factors", "factor_run_id"),
                ("backtests", "backtest_id"),
                ("evaluations", "evaluation_id"),
            )
        ]
    digests = {path: file_sha256(path) for path in paths}
    replay = ("rolling-replay", "--data-root", str(data), "--rolling-id", original)
    expected_id = original
    output = tmp_path / "replay"
    restored = command(*replay, "--output", str(output))
    assert restored["folds"] == first["folds"] and calls[-2:] == [v1, v1]
    assert ResultStore(output).read_experiment_record(original, "selection") == choice
    assert {path: file_sha256(path) for path in paths} == digests

    failed = tmp_path / "failed"
    for path, error in (
        (leaf / "manifest.json", "snapshot_unavailable"),
        (leaf / "market.parquet", "snapshot_unavailable"),
        (evidence, "evidence_unavailable"),
        (store.path("formulas", v1), "formula_version_unavailable"),
        (store.path("experiments", original).with_name("selection.json"), "invalid_rolling_result"),
    ):
        original_bytes = path.read_bytes()
        path.unlink()
        command(*replay, "--output", str(failed), error=error)
        path.write_bytes(original_bytes[:-1] + b"!")
        command(*replay, "--output", str(failed), error=error)
        path.write_bytes(original_bytes)
        assert not failed.exists()
    runtime = rolling_validation.source_provenance()
    monkeypatch.setattr(rolling_validation, "source_provenance", lambda: {**runtime, "python_version": "changed"})
    command(*replay, "--output", str(failed), error="runtime_mismatch")
    assert not failed.exists() and {path: file_sha256(path) for path in paths} == digests
    receipts = [
        json.loads(path.read_text(encoding="utf-8")) for path in store.root.glob("formula_failures/*/result.json")
    ]
    assert len(receipts) == 11 and all(row["rolling_id"] == original for row in receipts)
