import copy
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from finance_quant.persistence.results import PgResultStore
from finance_quant.storage import ResultStore
from finance_quant.web.comparisons import comparison_router, project_document
from support.fakes import make_app

TOTAL = -0.16201678769416406
PRICE_BENCHMARK = 0.032452314299515006
TOTAL_RETURN_BENCHMARK = 0.08187265424399648


def backtest(
    run_id,
    *,
    total=TOTAL,
    benchmark=PRICE_BENCHMARK,
    start="2025-03-03",
    end="2026-11-13",
    provenance=None,
    metadata=None,
    performance="daily-nav-performance-v3",
):
    provenance = {
        "data_version": "market-0efc5370",
        "manifest_sha256": "0efc5370700cc95ff27912a8295605f4236b4a02136aba1ae2115651d325bcb5",
        "code_sha256": "036bbf4577",
        "factors": [
            {"name": "momentum", "weight": 0.5, "window": 20},
            {"name": "value", "weight": 0.5, "window": None, "missing_policy": "zero"},
        ],
        **({"execution_run_id": None} if provenance is None else provenance),
    }
    document = {
        "summary": {
            "id": run_id,
            "name": run_id,
            "status": "succeeded",
            "start_date": start,
            "end_date": end,
            "created_at": "2026-11-19T00:00:00+00:00",
            "metrics": {
                "total_return": total,
                "benchmark_return": benchmark,
                "excess_return": None if benchmark is None or total is None else total - benchmark,
                "max_drawdown": -0.3257627931461061,
            },
        },
        "metadata": {
            "engine": "qlib",
            "engine_version": "0.9.7",
            "benchmark_name": "沪深300",
            "dataset_id": "historical-0efc5370",
            "provenance": provenance,
            **(metadata or {}),
        },
        "equity": [
            {"date": "2025-03-03", "equity": 1.0, "benchmark": 1.0, "drawdown": 0.0, "cost": 0.0},
            {"date": "2026-11-13", "equity": 0.84, "benchmark": 1.03, "drawdown": -0.32, "cost": 12.0},
        ],
        "trades": [{"date": "2025-03-04", "instrument": "SH600000", "cost": 5.0}],
        "positions": [{"date": "2025-03-04", "instrument": "SH600000"}],
        "tables": {"trades": {"parquet": "trades.parquet"}},
    }
    if performance:
        document["performance"] = {"version": performance, "metrics": {}}
    return document


def derived_from(source_id="market-qlib-src", *, total=TOTAL, **metadata):
    """A report produced from the source run's exact execution, new benchmark only."""

    return backtest(
        "benchmark-report",
        total=total,
        benchmark=TOTAL_RETURN_BENCHMARK,
        provenance={"execution_run_id": source_id},
        metadata={
            "execution_run_id": source_id,
            "benchmark_name": "沪深300全收益",
            "benchmark_provenance": {
                "code": "H00300.CSI",
                "version": "official-benchmark-close-v1",
                "series_sha256": "aae48286",
                "execution_run_id": source_id,
            },
            **(metadata or {}),
        },
    )


def client_for(tmp_path):
    return TestClient(make_app(tmp_path))


def publish(tmp_path, run_id, document):
    ResultStore(tmp_path).publish("backtests", run_id, document)
    return document


def test_comparison_requires_two_existing_and_different_runs(tmp_path):
    publish(tmp_path, "first", backtest("first"))
    client = client_for(tmp_path)
    assert client.get("/api/v1/backtest-comparisons?baseline_run_id=first&candidate_run_id=absent").status_code == 404
    assert client.get("/api/v1/backtest-comparisons?baseline_run_id=absent&candidate_run_id=first").status_code == 404
    assert client.get("/api/v1/backtest-comparisons?baseline_run_id=first&candidate_run_id=first").status_code == 422
    assert client.get("/api/v1/backtest-comparisons?baseline_run_id=first").status_code == 422


def test_same_execution_explains_the_reported_excess_gap_only(tmp_path):
    source = publish(tmp_path, "market-qlib-src", backtest("market-qlib-src"))
    publish(tmp_path, "benchmark-report", derived_from())
    response = client_for(tmp_path).get(
        "/api/v1/backtest-comparisons",
        params={"baseline_run_id": "benchmark-report", "candidate_run_id": "market-qlib-src"},
    )
    assert response.status_code == 200
    body = response.json()

    assert body["direction"] == "candidate_minus_baseline"
    assert body["relation"]["status"] == "same_execution"
    assert body["relation"]["derived_run_id"] == "benchmark-report"
    assert body["relation"]["source_run_id"] == "market-qlib-src"
    assert body["conclusion_states_cause"] is True
    assert "同一执行结果" in body["conclusion"]
    assert "基准" in body["conclusion"]

    metrics = {item["key"]: item for item in body["metrics"]}
    assert metrics["total_return"]["delta"] == pytest.approx(0)
    assert metrics["benchmark_return"]["delta"] == pytest.approx((PRICE_BENCHMARK - TOTAL_RETURN_BENCHMARK) * 100)
    assert metrics["excess_return"]["delta"] == pytest.approx((TOTAL_RETURN_BENCHMARK - PRICE_BENCHMARK) * 100)
    # The stored arithmetic is what the explanation rests on, to the last digit.
    assert metrics["excess_return"]["candidate"] == pytest.approx(source["summary"]["metrics"]["excess_return"])
    # The relation reports the same B − A direction as the metric table.
    assert body["relation"]["benchmark_delta_pp"] == pytest.approx(-4.942033994448147, abs=1e-9)
    assert body["relation"]["excess_delta_pp"] == pytest.approx(4.942033994448147, abs=1e-9)
    assert "-4.9420" in body["relation"]["explanation"]
    assert "不是策略交易变化的因果证明" in body["relation"]["explanation"]


def test_swapping_the_sides_flips_the_direction_but_keeps_the_evidence(tmp_path):
    publish(tmp_path, "market-qlib-src", backtest("market-qlib-src"))
    publish(tmp_path, "benchmark-report", derived_from())
    client = client_for(tmp_path)
    forward = client.get(
        "/api/v1/backtest-comparisons",
        params={"baseline_run_id": "market-qlib-src", "candidate_run_id": "benchmark-report"},
    ).json()
    assert forward["relation"]["status"] == "same_execution"
    metrics = {item["key"]: item for item in forward["metrics"]}
    assert metrics["benchmark_return"]["delta"] == pytest.approx(4.942033994448147, abs=1e-9)
    assert metrics["excess_return"]["delta"] == pytest.approx(-4.942033994448147, abs=1e-9)
    # The relation follows the same direction as the table after the swap.
    assert forward["relation"]["benchmark_delta_pp"] == pytest.approx(4.942033994448147, abs=1e-9)
    assert forward["relation"]["excess_delta_pp"] == pytest.approx(-4.942033994448147, abs=1e-9)
    assert "+4.9420" in forward["relation"]["explanation"]


def test_a_changed_code_hash_withdraws_the_single_cause(tmp_path):
    publish(tmp_path, "market-qlib-src", backtest("market-qlib-src"))
    publish(tmp_path, "benchmark-report", derived_from())
    changed = backtest(
        "benchmark-report",
        benchmark=TOTAL_RETURN_BENCHMARK,
        provenance={"execution_run_id": "market-qlib-src", "code_sha256": "ffffffff"},
        metadata={
            "execution_run_id": "market-qlib-src",
            "benchmark_provenance": {"code": "H00300.CSI", "execution_run_id": "market-qlib-src"},
        },
    )
    path = tmp_path / "backtests" / "benchmark-report" / "result.json"
    path.write_text(json.dumps(changed), encoding="utf-8")
    body = (
        client_for(tmp_path)
        .get(
            "/api/v1/backtest-comparisons",
            params={"baseline_run_id": "benchmark-report", "candidate_run_id": "market-qlib-src"},
        )
        .json()
    )
    assert {item["key"]: item for item in body["dimensions"]}["code"]["status"] == "different"
    assert body["relation"]["status"] == "conflict"
    assert body["conclusion_states_cause"] is False
    assert body["relation"]["explanation"] is None


def test_same_execution_claim_is_withdrawn_when_the_numbers_disagree(tmp_path):
    publish(tmp_path, "market-qlib-src", backtest("market-qlib-src"))
    publish(tmp_path, "benchmark-report", derived_from(total=-0.2))
    body = (
        client_for(tmp_path)
        .get(
            "/api/v1/backtest-comparisons",
            params={"baseline_run_id": "benchmark-report", "candidate_run_id": "market-qlib-src"},
        )
        .json()
    )
    assert body["relation"]["status"] == "conflict"
    assert body["relation"]["explanation"] is None
    assert body["conclusion_states_cause"] is False


def test_unrelated_pointer_means_independent_and_missing_pointer_is_unknown(tmp_path):
    publish(tmp_path, "left", backtest("left", provenance={"execution_run_id": "other-run"}))
    publish(tmp_path, "right", backtest("right", provenance={"execution_run_id": "another-run"}))
    client = client_for(tmp_path)
    independent = client.get(
        "/api/v1/backtest-comparisons", params={"baseline_run_id": "left", "candidate_run_id": "right"}
    ).json()
    assert independent["relation"]["status"] == "independent"
    assert independent["relation"]["explanation"] is None

    publish(tmp_path, "plain-a", backtest("plain-a", provenance={"execution_run_id": None}))
    publish(tmp_path, "plain-b", backtest("plain-b", provenance={"execution_run_id": None}))
    unknown = client.get(
        "/api/v1/backtest-comparisons", params={"baseline_run_id": "plain-a", "candidate_run_id": "plain-b"}
    ).json()
    assert unknown["relation"]["status"] == "unknown"


def test_missing_frozen_evidence_is_unknown_not_equal(tmp_path):
    publish(tmp_path, "a", backtest("a"))
    publish(tmp_path, "b", backtest("b"))
    body = (
        client_for(tmp_path)
        .get("/api/v1/backtest-comparisons", params={"baseline_run_id": "a", "candidate_run_id": "b"})
        .json()
    )
    dimensions = {item["key"]: item for item in body["dimensions"]}
    financials = dimensions["financials"]
    assert financials["status"] == "unknown"
    assert all(field["baseline"] is None and field["candidate"] is None for field in financials["fields"])
    assert all(field["status"] == "unknown" for field in financials["fields"])
    assert dimensions["market_data"]["status"] != "unknown"  # dataset version is stored on both sides


def test_changed_and_unknown_evidence_is_listed_without_a_cause(tmp_path):
    publish(tmp_path, "a", backtest("a"))
    publish(
        tmp_path,
        "b",
        backtest(
            "b",
            provenance={
                "code_sha256": "ffffffff",
                "factors": [{"name": "momentum", "weight": 1.0, "window": 60}],
            },
        ),
    )
    body = (
        client_for(tmp_path)
        .get("/api/v1/backtest-comparisons", params={"baseline_run_id": "a", "candidate_run_id": "b"})
        .json()
    )
    dimensions = {item["key"]: item for item in body["dimensions"]}
    assert dimensions["code"]["status"] == "different"
    assert dimensions["factors"]["status"] == "different"
    assert body["relation"]["status"] == "unknown"
    assert body["conclusion_states_cause"] is False
    assert "差异" in body["conclusion"]


def test_intervals_or_metric_versions_that_do_not_match_keep_values_but_flag_the_delta(tmp_path):
    publish(tmp_path, "a", backtest("a"))
    publish(tmp_path, "b", backtest("b", start="2025-04-01", performance="daily-nav-performance-v4"))
    body = (
        client_for(tmp_path)
        .get("/api/v1/backtest-comparisons", params={"baseline_run_id": "a", "candidate_run_id": "b"})
        .json()
    )
    comparability = body["comparability"]
    assert comparability["comparable"] is False
    assert comparability["interval"] == "different"
    assert comparability["metric_version"] == "different"
    assert len(comparability["reasons"]) == 2
    for metric in body["metrics"]:
        assert metric["baseline"] is not None  # stored values stay visible
        assert metric["comparable"] is False
        assert metric["delta"] is None  # never a comparable number


def test_missing_interval_or_metric_version_also_withholds_the_delta(tmp_path):
    publish(tmp_path, "known", backtest("known"))
    publish(tmp_path, "no-interval", backtest("no-interval", start=None, end=None))
    publish(tmp_path, "no-version", backtest("no-version", performance=None))
    client = client_for(tmp_path)

    missing_interval = client.get(
        "/api/v1/backtest-comparisons",
        params={"baseline_run_id": "known", "candidate_run_id": "no-interval"},
    ).json()
    assert missing_interval["comparability"]["interval"] == "unknown"
    assert missing_interval["comparability"]["comparable"] is False
    assert all(metric["delta"] is None for metric in missing_interval["metrics"])
    assert missing_interval["metrics"][0]["baseline"] is not None

    missing_version = client.get(
        "/api/v1/backtest-comparisons",
        params={"baseline_run_id": "known", "candidate_run_id": "no-version"},
    ).json()
    assert missing_version["comparability"]["metric_version"] == "unknown"
    assert missing_version["comparability"]["comparable"] is False
    assert all(metric["delta"] is None for metric in missing_version["metrics"])
    assert missing_version["relation"]["status"] == "unknown"
    assert missing_version["conclusion_states_cause"] is False


def test_comparison_reads_one_narrow_projection_and_never_writes(tmp_path):
    document = publish(tmp_path, "a", backtest("a"))
    publish(tmp_path, "b", derived_from())
    stored = (tmp_path / "backtests" / "a" / "result.json").read_bytes()

    class NoDetailReads(ResultStore):
        def get(self, category, run_id, *, hydrate=True):  # pragma: no cover - must not be used
            raise AssertionError("comparison must not load the stored detail document")

    store = NoDetailReads(tmp_path)
    projection = project_document(store, "backtests", "a")
    for key in ("equity", "trades", "positions", "tables"):
        assert key not in projection
    assert projection["summary"] == copy.deepcopy(document["summary"])

    app = FastAPI()
    app.include_router(comparison_router(store))
    response = TestClient(app).get(
        "/api/v1/backtest-comparisons", params={"baseline_run_id": "a", "candidate_run_id": "b"}
    )
    assert response.status_code == 200
    assert "equity" not in response.json()
    assert (tmp_path / "backtests" / "a" / "result.json").read_bytes() == stored


def test_a_damaged_document_is_reported_as_unreadable_rather_than_missing(tmp_path):
    publish(tmp_path, "good", backtest("good"))
    broken = tmp_path / "backtests" / "broken"
    broken.mkdir(parents=True)
    (broken / "result.json").write_text('{"summary": 1}', encoding="utf-8")
    client = client_for(tmp_path)
    response = client.get(
        "/api/v1/backtest-comparisons", params={"baseline_run_id": "good", "candidate_run_id": "broken"}
    )
    assert response.status_code == 409


def report_of(run_id, source_id, benchmark):
    """A derived report of ``source_id`` with its own benchmark."""

    return backtest(
        run_id,
        benchmark=benchmark,
        provenance={"execution_run_id": source_id},
        metadata={
            "execution_run_id": source_id,
            "benchmark_provenance": {"code": "X", "execution_run_id": source_id},
        },
    )


def test_two_reports_of_one_execution_are_siblings_not_a_cause(tmp_path):
    publish(tmp_path, "market-qlib-src", backtest("market-qlib-src"))
    publish(tmp_path, "report-a", report_of("report-a", "market-qlib-src", TOTAL_RETURN_BENCHMARK))
    publish(tmp_path, "report-b", report_of("report-b", "market-qlib-src", 0.05))
    body = (
        client_for(tmp_path)
        .get(
            "/api/v1/backtest-comparisons",
            params={"baseline_run_id": "report-a", "candidate_run_id": "report-b"},
        )
        .json()
    )
    assert body["relation"]["status"] == "sibling"
    assert body["relation"]["explanation"] is None
    assert body["conclusion_states_cause"] is False
    assert "同一个执行结果" in body["conclusion"]
    assert body["relation"]["execution_pointer"] == "market-qlib-src"


def test_total_return_must_match_exactly(tmp_path):
    publish(tmp_path, "market-qlib-src", backtest("market-qlib-src"))
    # Even a 1e-13 difference means the two stored totals are not the same result.
    publish(tmp_path, "benchmark-report", derived_from(total=TOTAL + 1e-13))
    near = (
        client_for(tmp_path)
        .get(
            "/api/v1/backtest-comparisons",
            params={"baseline_run_id": "benchmark-report", "candidate_run_id": "market-qlib-src"},
        )
        .json()
    )
    assert near["relation"]["status"] == "conflict"
    assert near["relation"]["explanation"] is None

    publish(tmp_path, "drifted", derived_from(total=TOTAL + 1e-6))
    far = (
        client_for(tmp_path)
        .get(
            "/api/v1/backtest-comparisons",
            params={"baseline_run_id": "drifted", "candidate_run_id": "market-qlib-src"},
        )
        .json()
    )
    assert far["relation"]["status"] == "conflict"
    assert far["relation"]["explanation"] is None


def test_postgres_projection_reads_one_row_and_no_detail_table(tmp_path):
    statements = []

    class FakeResult:
        def __init__(self, row):
            self._row = row

        def mappings(self):
            return self

        def first(self):
            return self._row

    class FakeConnection:
        def execute(self, statement, *args, **kwargs):
            statements.append(str(statement))
            return FakeResult(
                {
                    "metadata": {"engine": "qlib"},
                    "summary": {"metrics": {"total_return": 0.1}},
                    "dataset_id": "historical-x",
                    "performance_version": "v3",
                }
            )

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class FakeEngine:
        def connect(self):
            return FakeConnection()

    store = PgResultStore(root=tmp_path, engine=FakeEngine(), storage_root=tmp_path)
    projection = store.project("backtests", "run-1")

    assert projection["summary"] == {"metrics": {"total_return": 0.1}}
    assert projection["metadata"]["dataset_id"] == "historical-x"
    assert projection["performance"] == {"version": "v3"}
    assert len(statements) == 1, "one projection is one statement"
    sql = statements[0].lower()
    assert "research_run" in sql
    # Explicit column projection: no wildcard, no detail tables.
    assert "select" in sql and "*" not in sql.split("from")[0]
    for column in ("summary", "metadata", "dataset_id", "performance"):
        assert column in sql
    for table in ("run_equity", "run_trade", "run_metrics", "run_artifact"):
        assert table not in sql, f"{table} must not be read by the projection"


def test_a_fixed_pool_backtest_borrows_the_recipe_of_its_frozen_factor_run(tmp_path):
    publish(
        tmp_path,
        "fixed-backtest",
        backtest(
            "fixed-backtest",
            provenance={"factor_run_id": "fixed-factors", "factors": None},
        ),
    )
    ResultStore(tmp_path).publish(
        "factors",
        "fixed-factors",
        {
            "metadata": {
                "id": "fixed-factors",
                "factor_config": {"factors": [{"name": "momentum", "weight": 1.0, "window": 20}]},
            },
            "history": [],
        },
    )
    publish(
        tmp_path,
        "with-recipe",
        backtest(
            "with-recipe",
            provenance={"factors": [{"name": "momentum", "weight": 1.0, "window": 20}]},
        ),
    )
    body = (
        client_for(tmp_path)
        .get(
            "/api/v1/backtest-comparisons",
            params={"baseline_run_id": "fixed-backtest", "candidate_run_id": "with-recipe"},
        )
        .json()
    )
    factors = {item["key"]: item for item in body["dimensions"]}["factors"]
    recipe = next(field for field in factors["fields"] if field["label"] == "因子与权重")
    assert recipe["status"] == "same"
    assert "momentum" in recipe["baseline"]


def test_contradicting_execution_pointers_cannot_claim_a_cause(tmp_path):
    # A says it came from B, but B says it came from someone else.
    publish(tmp_path, "b-run", backtest("b-run", provenance={"execution_run_id": "other-run"}))
    publish(tmp_path, "a-report", report_of("a-report", "b-run", PRICE_BENCHMARK))
    client = client_for(tmp_path)
    crossed = client.get(
        "/api/v1/backtest-comparisons",
        params={"baseline_run_id": "a-report", "candidate_run_id": "b-run"},
    ).json()
    assert crossed["relation"]["status"] == "conflict"
    assert crossed["relation"]["explanation"] is None

    # Circular: each side points at the other.
    publish(tmp_path, "a", report_of("a", "b", PRICE_BENCHMARK))
    publish(tmp_path, "b", report_of("b", "a", TOTAL_RETURN_BENCHMARK))
    circular = client.get(
        "/api/v1/backtest-comparisons", params={"baseline_run_id": "a", "candidate_run_id": "b"}
    ).json()
    assert circular["relation"]["status"] == "conflict"
    assert circular["relation"]["explanation"] is None


def test_legacy_benchmark_code_is_read_from_the_older_metadata_paths(tmp_path):
    publish(tmp_path, "legacy", backtest("legacy", metadata={"provenance": {"benchmark": "000300.SH"}}))
    publish(tmp_path, "modern", backtest("modern", metadata={"config": {"benchmark": "000300.SH"}}))
    body = (
        client_for(tmp_path)
        .get("/api/v1/backtest-comparisons", params={"baseline_run_id": "legacy", "candidate_run_id": "modern"})
        .json()
    )
    benchmark = {item["key"]: item for item in body["dimensions"]}["benchmark"]
    code = next(field for field in benchmark["fields"] if field["label"] == "基准代码")
    assert code["status"] == "same"
    assert code["baseline"] == "000300.SH" and code["candidate"] == "000300.SH"


def test_fixed_pool_recipe_keeps_the_stored_neutralization(tmp_path):
    publish(
        tmp_path,
        "fixed-backtest",
        backtest("fixed-backtest", provenance={"factor_run_id": "fixed-factors", "factors": None}),
    )
    ResultStore(tmp_path).publish(
        "factors",
        "fixed-factors",
        {
            "metadata": {
                "id": "fixed-factors",
                "factor_config": {"factors": [{"name": "momentum", "weight": 1.0, "window": 20}]},
                "neutralization": {"mode": "industry", "industry_level": "L1"},
            },
            "history": [],
        },
    )
    publish(
        tmp_path,
        "peer",
        backtest(
            "peer",
            provenance={
                "factors": [{"name": "momentum", "weight": 1.0, "window": 20}],
                "neutralization": {"mode": "industry", "industry_level": "L1"},
            },
        ),
    )
    body = (
        client_for(tmp_path)
        .get(
            "/api/v1/backtest-comparisons",
            params={"baseline_run_id": "fixed-backtest", "candidate_run_id": "peer"},
        )
        .json()
    )
    factors = {item["key"]: item for item in body["dimensions"]}["factors"]
    neutralization = next(field for field in factors["fields"] if field["label"] == "中性化")
    assert neutralization["status"] == "same"
    assert "industry" in neutralization["baseline"]


def test_a_broken_factor_run_leaves_the_recipe_unknown_instead_of_failing(tmp_path):
    publish(
        tmp_path,
        "fixed-backtest",
        backtest("fixed-backtest", provenance={"factor_run_id": "broken-factors", "factors": None}),
    )
    broken = tmp_path / "factors" / "broken-factors"
    broken.mkdir(parents=True)
    (broken / "result.json").write_text("not json", encoding="utf-8")
    publish(tmp_path, "peer", backtest("peer"))
    response = client_for(tmp_path).get(
        "/api/v1/backtest-comparisons",
        params={"baseline_run_id": "fixed-backtest", "candidate_run_id": "peer"},
    )
    assert response.status_code == 200
    factors = {item["key"]: item for item in response.json()["dimensions"]}["factors"]
    assert all(field["status"] != "same" for field in factors["fields"])


def test_nested_corruption_is_a_conflict_response_not_a_server_error(tmp_path):
    publish(tmp_path, "good", backtest("good"))
    bad_metrics = backtest("bad-metrics")
    bad_metrics["summary"]["metrics"] = "corrupt"
    publish(tmp_path, "bad-metrics", bad_metrics)
    bad_benchmark = backtest("bad-benchmark")
    bad_benchmark["metadata"]["benchmark"] = ["corrupt"]
    publish(tmp_path, "bad-benchmark", bad_benchmark)
    client = client_for(tmp_path)
    for broken in ("bad-metrics", "bad-benchmark"):
        response = client.get(
            "/api/v1/backtest-comparisons",
            params={"baseline_run_id": "good", "candidate_run_id": broken},
        )
        assert response.status_code == 409


def test_conflicting_pointers_inside_one_result_block_the_cause(tmp_path):
    publish(tmp_path, "source", backtest("source"))
    publish(tmp_path, "peer", backtest("peer"))
    contradictory = backtest(
        "contradictory",
        provenance={"execution_run_id": "source", "factor_run_id": None},
        metadata={
            "execution_run_id": "source",
            "benchmark_provenance": {"code": "X", "execution_run_id": "peer"},
        },
    )
    publish(tmp_path, "contradictory", contradictory)
    body = (
        client_for(tmp_path)
        .get(
            "/api/v1/backtest-comparisons",
            params={"baseline_run_id": "contradictory", "candidate_run_id": "source"},
        )
        .json()
    )
    assert body["relation"]["status"] == "conflict"
    assert body["relation"]["explanation"] is None


def test_legacy_benchmark_mapping_is_read_as_a_code(tmp_path):
    publish(tmp_path, "legacy", backtest("legacy", metadata={"provenance": {"benchmark": {"code": "000300.SH"}}}))
    publish(tmp_path, "modern", backtest("modern", metadata={"config": {"benchmark": "000300.SH"}}))
    body = (
        client_for(tmp_path)
        .get("/api/v1/backtest-comparisons", params={"baseline_run_id": "legacy", "candidate_run_id": "modern"})
        .json()
    )
    benchmark = {item["key"]: item for item in body["dimensions"]}["benchmark"]
    code = next(field for field in benchmark["fields"] if field["label"] == "基准代码")
    assert code["baseline"] == "000300.SH"
    assert code["status"] == "same"


def test_a_related_factor_archive_with_a_non_mapping_metadata_body_stays_unknown(tmp_path):
    publish(
        tmp_path,
        "fixed-backtest",
        backtest("fixed-backtest", provenance={"factor_run_id": "odd-factors", "factors": None}),
    )
    for run_id, body in (
        ("odd-factors", {"metadata": ["corrupt"], "history": []}),
        ("null-factors", {"metadata": None, "history": []}),
        ("list-factors", ["not-a-mapping"]),
    ):
        ResultStore(tmp_path).publish("factors", run_id, body)
    publish(tmp_path, "peer", backtest("peer"))
    client = client_for(tmp_path)
    for run_id in ("odd-factors", "null-factors", "list-factors"):
        # Point the same backtest at each odd archive in turn.
        document = backtest("fixed-backtest", provenance={"factor_run_id": run_id, "factors": None})
        (tmp_path / "backtests" / "fixed-backtest" / "result.json").write_text(json.dumps(document), encoding="utf-8")
        response = client.get(
            "/api/v1/backtest-comparisons",
            params={"baseline_run_id": "fixed-backtest", "candidate_run_id": "peer"},
        )
        assert response.status_code == 200, run_id
        factors = {item["key"]: item for item in response.json()["dimensions"]}["factors"]
        assert all(field["status"] != "same" for field in factors["fields"]), run_id


def test_a_top_level_non_object_document_is_a_conflict_response(tmp_path):
    publish(tmp_path, "good", backtest("good"))
    for run_id, body in (("numeric", "123"), ("text", '"broken"'), ("list", "[1, 2, 3]")):
        target = tmp_path / "backtests" / run_id
        target.mkdir(parents=True)
        (target / "result.json").write_text(body, encoding="utf-8")
    client = client_for(tmp_path)
    for run_id in ("numeric", "text", "list"):
        response = client.get(
            "/api/v1/backtest-comparisons",
            params={"baseline_run_id": "good", "candidate_run_id": run_id},
        )
        assert response.status_code == 409, run_id


def test_qlib_style_benchmark_codes_compare_equal(tmp_path):
    publish(tmp_path, "legacy", backtest("legacy", metadata={"provenance": {"benchmark": "SH000300"}}))
    publish(tmp_path, "modern", backtest("modern", metadata={"benchmark": {"code": "000300.SH"}}))
    body = (
        client_for(tmp_path)
        .get("/api/v1/backtest-comparisons", params={"baseline_run_id": "legacy", "candidate_run_id": "modern"})
        .json()
    )
    benchmark = {item["key"]: item for item in body["dimensions"]}["benchmark"]
    code = next(field for field in benchmark["fields"] if field["label"] == "基准代码")
    assert code["status"] == "same"
    assert code["baseline"] == code["candidate"] == "000300.SH"


def test_a_string_provenance_or_nan_metric_is_a_conflict_response(tmp_path):
    publish(tmp_path, "good", backtest("good"))
    string_provenance = backtest("string-provenance", provenance={"execution_run_id": "x"})
    string_provenance["metadata"]["provenance"] = "corrupt"
    publish(tmp_path, "string-provenance", string_provenance)
    nan_metrics = backtest("nan-metrics")
    nan_metrics["summary"]["metrics"]["total_return"] = float("nan")
    target = tmp_path / "backtests" / "nan-metrics"
    target.mkdir(parents=True)
    (target / "result.json").write_text(json.dumps(nan_metrics), encoding="utf-8")
    client = client_for(tmp_path)
    for run_id in ("string-provenance", "nan-metrics"):
        response = client.get(
            "/api/v1/backtest-comparisons",
            params={"baseline_run_id": "good", "candidate_run_id": run_id},
        )
        assert response.status_code == 409, run_id
