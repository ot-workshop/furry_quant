import pytest
from fastapi.testclient import TestClient

from finance_quant.api import create_app
from finance_quant.sample import sample_factors, sample_prices
from finance_quant.storage import ResultStore


def test_immutable_results_and_identifier_boundaries(tmp_path):
    store = ResultStore(tmp_path)
    document = {"a": 1}
    store.publish("factors", "fixed", document)
    store.publish("factors", "fixed", document)
    with pytest.raises(FileExistsError):
        store.publish("factors", "fixed", {"a": 2})
    with pytest.raises(ValueError):
        store.path("factors", "../../secrets")
    with pytest.raises(ValueError):
        store.publish("factors", "nan", {"bad": float("nan")})
    assert store.get("factors", "fixed") == document
    store.publish("factors", "ordered", {"a": 1, "b": 2})
    store.publish("factors", "ordered", {"b": 2, "a": 1})


def test_empty_api_and_validation(tmp_path):
    (tmp_path / "backtests" / "interrupted").mkdir(parents=True)
    client = TestClient(create_app(tmp_path))
    assert client.get("/api/v1/backtests").json() == {"items": [], "total": 0}
    assert client.get("/api/v1/backtests/absent").status_code == 404
    assert client.get("/api/v1/backtests/absent/trades?limit=-1").status_code == 422
    assert client.get("/api/v1/unknown").status_code == 404
    assert client.post("/api/v1/backtests").status_code == 405


def test_factor_api_pagination_ranking_and_history(tmp_path):
    factors = sample_factors(sample_prices())
    ResultStore(tmp_path).publish("factors", "demo-factors", factors)
    client = TestClient(create_app(tmp_path))
    listing = client.get("/api/v1/factor-runs").json()
    assert listing["items"][0]["is_synthetic"] is True
    assert "2025-01-06" in listing["items"][0]["available_dates"]
    response = client.get("/api/v1/factor-runs/demo-factors/scores?limit=1").json()
    assert response["total"] == 3
    assert len(response["items"]) == 1
    assert response["items"][0]["rank"] == 1
    assert response["metadata"]["score_unit"] == "percentile_0_100"
    assert client.get("/api/v1/factor-runs/demo-factors/scores?sort_by=invalid").status_code == 422
    instrument = response["items"][0]["instrument"]
    history = client.get(f"/api/v1/instruments/{instrument}/factor-history").json()
    assert len(history["items"]) == 10
    assert history["items"][0]["date"] == "2025-01-06"
    assert client.get(f"/api/v1/instruments/{instrument}/factor-history?scheme=other").json()["items"] == []
    previous = client.get("/api/v1/factor-runs/demo-factors/scores?date=2025-01-06").json()
    assert previous["metadata"]["as_of"] == "2025-01-06"
    assert all(row["date"] == "2025-01-06" for row in previous["items"])
    assert client.get("/api/v1/factor-runs/demo-factors/scores?date=bad").status_code == 422


def test_history_requires_explicit_run_when_scheme_has_multiple_versions(tmp_path):
    factors = sample_factors(sample_prices())
    store = ResultStore(tmp_path)
    store.publish("factors", "first", factors)
    factors["metadata"]["id"] = "second"
    store.publish("factors", "second", factors)
    client = TestClient(create_app(tmp_path))
    path = "/api/v1/instruments/SH600000/factor-history"
    assert client.get(path).status_code == 409
    selected = client.get(path + "?run_id=second").json()
    assert all(row["run_id"] == "second" for row in selected["items"])


def test_factor_scores_do_not_depend_on_future_prices():
    original = sample_prices()
    baseline = sample_factors(original)
    changed = original.copy()
    changed.loc[changed["date"] > "2025-01-10", "close"] *= 3
    altered = sample_factors(changed)
    before = lambda result: [row for row in result["history"] if row["date"] <= "2025-01-10"]
    assert before(baseline) == before(altered)
