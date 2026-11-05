import copy

import pandas as pd
import pytest

from finance_quant.application.research_readiness import validate_warmup
from finance_quant.application.research_requests import research_options
from finance_quant.data.market import load_market
from finance_quant.research.registry import FactorConfig, FactorSpec
from support.fakes import fixture_root, make_client


@pytest.mark.parametrize(
    "spec,required",
    [
        (FactorSpec("value", 1.0), 1),
        (FactorSpec("momentum", 1.0, 20, skip=5), 21),
        (FactorSpec("max_drawdown", 1.0, 60), 61),
        (FactorSpec("medium_momentum", 1.0, 126, skip=21), 127),
        (FactorSpec("amount_anomaly", 1.0, 20, baseline_window=60), 80),
        (FactorSpec("amount_anomaly", 1.0, 252, baseline_window=252), 504),
    ],
)
def test_exact_history_boundary_uses_prior_signal_day_without_adding_skip(spec, required):
    dates = pd.date_range("2025-01-01", "2027-12-31")
    calendar = pd.DataFrame(
        {"date": dates, "is_open": (dates.weekday < 5) & ~dates.isin(pd.to_datetime(["2025-01-01", "2025-01-02"]))}
    )
    sessions = calendar.loc[calendar.is_open, "date"].tolist()
    config = FactorConfig((spec,))
    with pytest.raises(ValueError, match=f"至少需要{required}个"):
        validate_warmup("2025-01-01", "2027-12-31", sessions[required - 1], config, calendar)
    validate_warmup("2025-01-01", "2027-12-31", sessions[required], config, calendar)


def test_cached_history_before_requested_data_start_is_not_borrowed():
    dates = pd.date_range("2024-01-01", "2026-09-30")
    calendar = pd.DataFrame({"date": dates, "is_open": dates.weekday < 5})
    with pytest.raises(ValueError, match="历史预热不足"):
        validate_warmup(
            "2025-06-02", "2026-09-30", "2025-07-15", FactorConfig((FactorSpec("medium_momentum", 1.0),)), calendar
        )


def test_unknown_calendar_rejects_impossible_span_without_guessing_a_trade_date():
    config = FactorConfig((FactorSpec("medium_momentum", 1.0),))
    with pytest.raises(ValueError, match="127个.*61个自然日"):
        validate_warmup("2025-01-01", "2026-09-30", "2025-03-03", config)
    validate_warmup("2024-01-01", "2026-09-30", "2025-03-03", config)


def test_invalid_warmup_fails_api_before_queue_or_projection(workspace):
    data, runs, request = workspace
    request = copy.deepcopy(request)
    request["backtest"]["start"] = "2025-01-02"
    request["factors"] = [{"name": "medium_momentum", "weight": 1.0, "window": 126, "skip": 21}]
    client, service = make_client(data, runs)
    response = client.post("/api/v1/research/tasks", json=request, headers={"Idempotency-Key": "warmup-no-queue"})
    assert response.status_code == 422
    assert "127个" in response.json()["detail"] and "历史预热不足" in response.json()["detail"]
    assert service.store.list()["total"] == 0
    assert not runs.exists() and not (data / "research/subsets").exists()


def test_options_mark_derived_subsets_without_exposing_paths(workspace):
    from pathlib import Path

    from finance_quant.application.research_requests import project_snapshot

    data, runs, request = workspace
    parent = load_market(fixture_root(data))[3]
    subset = load_market(project_snapshot(Path(parent["snapshot_dir"]), tuple(request["data"]["codes"]), data))[3]
    options = research_options(data, runs)
    rows = {s["id"]: s for s in options["snapshots"]}
    assert rows[parent["id"]]["is_subset"] is False and rows[subset["id"]]["is_subset"] is True
    assert options["snapshots"][0]["id"] == parent["id"], "shared exports precede derived subsets"
    assert options["defaults"]["data"]["snapshot_id"] == parent["id"]
    assert not any("dir" in key or "path" in key for row in rows.values() for key in row)
