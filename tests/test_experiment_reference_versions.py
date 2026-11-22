"""A queued experiment must not use financial or industry versions published after its POST."""

from copy import deepcopy
from pathlib import Path
from shutil import rmtree

import pandas as pd
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient
from test_experiments import experiment_request
from test_factor_selection_flow import source
from test_industry_neutralization import history
from test_managed_data import Provider

from finance_quant.application import experiment_references
from finance_quant.application.market_requests import pool_industry_display
from finance_quant.data.financials import FinancialStore, financial_root
from finance_quant.data.industries import industry_root, publish_industries
from finance_quant.research.neutralization import parse_neutralization
from finance_quant.storage import ResultStore
from support.fakes import make_app, make_service, run_claimed
from support.financial import income, publish, seed_earnings
from support.fixed_pool import publish_fixture_snapshot

CODES = ("600000.SH", "000001.SZ", "600519.SH")
SYMBOLS = ("SH600000", "SZ000001", "SH600519")


@pytest.fixture
def workspace_ref(tmp_path):
    data, runs = tmp_path / "data", tmp_path / "runs"
    snapshot = publish_fixture_snapshot(data, CODES, "20241202", "20250228", provider=Provider())
    service = make_service(runs, data)
    client = TestClient(make_app(runs, data, service=service), base_url="http://127.0.0.1")
    return service, client, experiment_request(snapshot["id"])


def submit(client, document, key="experiment-reference-pins"):
    return client.post("/api/v1/research/experiments", json=document, headers={"Idempotency-Key": key})


def publish_a(service):
    financial = seed_earnings(financial_root(service.data_root), CODES).version
    industry = publish_industries(history(SYMBOLS).frame).manifest["id"]
    return financial, industry


def publish_b(service):
    financial = publish(
        financial_root(service.data_root),
        [
            income(code, period=period, announce=announce, n_income_attr_p=400_000.0 * (i + 1))
            for i, code in enumerate(CODES)
            for period, announce in (("20230930", "20231025"), ("20231231", "20240320"), ("20240930", "20241025"))
        ],
        seen_on="2024-10-25",
    )["id"]
    industry = publish_industries(history(SYMBOLS, switch=True).frame).manifest["id"]
    return financial, industry


def claim_and_run(service, identifier):
    assert service.store.claim() == identifier
    return run_claimed(service, identifier)


@pytest.mark.parametrize("industry_present", [True, False], ids=["A-to-B", "industry-absent-to-B"])
def test_reference_versions_survive_a_newer_publish(workspace_ref, industry_present):
    service, client, document = workspace_ref
    financial = seed_earnings(financial_root(service.data_root), CODES).version
    industry = publish_industries(history(SYMBOLS).frame).manifest["id"] if industry_present else None
    response = submit(client, document)
    assert response.status_code == 202, response.text
    identifier = response.json()["id"]
    row = service.store.get(identifier, private=True)
    assert row["execution"]["reference_versions"] == {
        "source": "submission",
        "financial": financial,
        "industry": industry,
        "filter_industry": None,
    }
    newer_financial, newer_industry = publish_b(service)
    assert financial != newer_financial and industry != newer_industry
    task = claim_and_run(service, identifier)
    assert task["status"] == "succeeded", task["error"]
    prepared = service.store.get(identifier, private=True)
    assert prepared["request"] == row["request"] == document
    assert submit(client, document).json()["id"] == identifier
    assert service.store.list()["total"] == 1
    assert prepared["execution"].get("financials", {}).get("version") == financial
    store = ResultStore(service.runs_root)
    saved = store.get("experiments", task["result"]["experiment_run_id"], hydrate=False)
    assert saved["metadata"]["provenance"].get("financial_version") == financial
    assert len(saved["rows"]) == 5
    for entry in saved["rows"]:
        provenance = store.get("factors", entry["factor_run_id"], hydrate=False)["metadata"]["provenance"]
        assert provenance.get("industry_version") == industry
        if entry["candidate_id"] == "value":
            assert provenance.get("financial_version") == financial


def test_missing_financial_version_is_not_filled_by_later_publish(workspace_ref):
    service, client, document = workspace_ref
    document["plan"]["candidates"][0]["factors"][0]["missing_policy"] = "exclude"
    response = submit(client, document)
    assert response.status_code == 202, response.text
    identifier = response.json()["id"]
    pending = service.store.get(identifier, private=True)["execution"]
    assert pending["reference_versions"]["financial"] is None
    publish_b(service)
    task = claim_and_run(service, identifier)
    assert task["status"] == "failed" and task["error_details"]["stage"] == "prepare"
    assert "提交时未有可用财务版本" in task["error"]
    assert task["result"] is None
    assert service.store.get(identifier, private=True)["execution"] == pending
    assert not any((service.data_root / "research/subsets").glob("*/manifest.json"))


def test_reference_post_reads_only_bounded_pointers_and_replay_reads_neither(workspace_ref, monkeypatch):
    service, client, document = workspace_ref
    financial, industry = publish_a(service)
    paths = {financial_root(service.data_root) / "current.json", industry_root() / "current.json"}
    original_open = Path.open
    touched = []

    def forbidden(*args, **kwargs):
        pytest.fail("POST used version content or Parquet instead of the small pointer")

    def trace(path, *args, **kwargs):
        if path in paths:
            touched.append(path)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", trace)
    monkeypatch.setattr(FinancialStore, "version", forbidden)
    monkeypatch.setattr(FinancialStore, "current", forbidden)
    monkeypatch.setattr(pd, "read_parquet", forbidden)
    monkeypatch.setattr(pq, "read_table", forbidden)
    response = submit(client, document)
    assert response.status_code == 202, response.text
    identifier = response.json()["id"]
    row = service.store.get(identifier, private=True)
    assert row["execution"]["reference_versions"]["financial"] == financial
    assert row["execution"]["reference_versions"]["industry"] == industry
    assert len(touched) == 2 and set(touched) == paths
    monkeypatch.setattr(experiment_references, "_pointer", forbidden)
    assert submit(client, document).json()["id"] == identifier
    assert submit(client, {**document, "name": "modified"}).status_code == 409
    assert service.store.list()["total"] == 1


def test_reference_post_rejects_oversized_pointer_without_enqueue(workspace_ref):
    service, client, document = workspace_ref
    publish_a(service)
    (financial_root(service.data_root) / "current.json").write_bytes(b"x" * 4097)
    response = submit(client, document)
    assert response.status_code == 422 and "财务" in response.json()["detail"]
    assert service.store.list()["total"] == 0


@pytest.mark.parametrize("damage", ["financial-manifest", "financial-object", "industry-version", "industry-object"])
def test_unavailable_bound_reference_fails_before_prepared_or_projection(workspace_ref, damage, monkeypatch):
    service, client, document = workspace_ref
    financial, industry = publish_a(service)
    response = submit(client, document)
    assert response.status_code == 202, response.text
    identifier = response.json()["id"]
    execution = deepcopy(service.store.get(identifier, private=True)["execution"])
    publish_b(service)
    finances = financial_root(service.data_root)
    if damage == "financial-manifest":
        (finances / "versions" / f"{financial}.json").unlink()
    elif damage == "financial-object":
        manifest = FinancialStore(finances).version(financial)
        path = finances / "objects" / f"{manifest['stocks'][CODES[0]]['object']}.parquet"
        path.write_bytes(b"broken parquet")
    elif damage == "industry-version":
        rmtree(industry_root() / industry)
    else:
        (industry_root() / industry / "intervals.parquet").write_bytes(b"broken parquet")

    def forbidden(*args, **kwargs):
        pytest.fail("invalid reference must not reach the preparation write")

    monkeypatch.setattr(service.store, "save_prepared", forbidden)
    before = {p for p in service.data_root.rglob("*") if p.is_file()}
    task = claim_and_run(service, identifier)
    assert task["status"] == "failed" and task["error_details"]["stage"] == "prepare"
    assert ("财务" if damage.startswith("financial-") else "行业") in task["error"]
    assert task["result"] is None
    assert service.store.get(identifier, private=True)["execution"] == execution
    assert {p for p in service.data_root.rglob("*") if p.is_file()} == before
    store = ResultStore(service.runs_root)
    assert all(not store.list(category) for category in ("experiments", "backtests", "factors", "evaluations"))


def test_selection_handoff_uses_its_source_versions_not_submission_current(tmp_path):
    originals = {}

    def runtime(runs, data):
        originals["financial"] = publish(
            financial_root(data),
            [
                income(code, period="20231231", announce="20240103", n_income_attr_p=100 + i)
                for i, code in enumerate(CODES)
            ],
            seen_on="2024-01-03",
        )["id"]
        originals["industry"] = publish_industries(history(SYMBOLS).frame).manifest["id"]
        service = make_service(runs, data)
        client = TestClient(make_app(runs, data, service=service), base_url="http://127.0.0.1")
        return service, client, ResultStore(runs)

    client, service, store, evaluation_id, document, _ = source(tmp_path, runtime=runtime)
    financial, industry = publish_b(service)
    assert financial != originals["financial"] and industry != originals["industry"]
    response = submit(client, document)
    assert response.status_code == 202, response.text
    identifier = response.json()["id"]
    assert service.store.get(identifier, private=True)["execution"]["reference_versions"] == {"source": "selection"}
    assert document["neutralization"]["industry_version"] == originals["industry"]
    task = claim_and_run(service, identifier)
    assert task["status"] == "succeeded", task["error"]
    saved = store.get("experiments", task["result"]["experiment_run_id"], hydrate=False)
    assert saved["metadata"]["factor_selection"]["evaluation_id"] == evaluation_id
    assert saved["metadata"]["provenance"]["financial_version"] == originals["financial"]
    for entry in saved["rows"]:
        provenance = store.get("factors", entry["factor_run_id"], hydrate=False)["metadata"]["provenance"]
        assert provenance["industry_version"] == originals["industry"]


@pytest.mark.parametrize(
    "pin_scoring,pin_filter",
    [(True, True), (False, True), (True, False), (False, False)],
    ids=["distinct-pins", "filter-only", "scoring-only", "neither"],
)
def test_scoring_and_filter_industries_stay_separate(workspace_ref, pin_scoring, pin_filter):
    service, client, document = workspace_ref
    seed_earnings(financial_root(service.data_root), CODES)
    scoring = None
    if pin_scoring:
        scoring = publish_industries(history(SYMBOLS).frame).manifest["id"]
        document["neutralization"] = {"mode": "off", "industry_version": scoring}
    if pin_filter:
        filter_frame = history(SYMBOLS).frame.assign(
            industry_code=["KEEP", "DROP", "KEEP"], industry_name=["Keep", "Drop", "Keep"]
        )
        filter_version = publish_industries(filter_frame).manifest["id"]
        document["data"]["filters"] = {
            "exclude_st": False,
            "min_listing_days": 0,
            "industries": ["KEEP"],
            "industry_version": filter_version,
        }
    else:
        filter_version = None
    accepted = submit(client, document)
    assert accepted.status_code == 202, accepted.text
    identifier = accepted.json()["id"]
    expected_score_version = scoring if pin_scoring else filter_version
    pins = service.store.get(identifier, private=True)["execution"]["reference_versions"]
    assert pins["industry"] == expected_score_version
    assert pins["filter_industry"] == filter_version
    _, latest = publish_b(service)
    assert latest not in {scoring, filter_version}

    task = claim_and_run(service, identifier)
    assert task["status"] == "succeeded", task["error"]
    store = ResultStore(service.runs_root)
    saved = store.get("experiments", task["result"]["experiment_run_id"], hydrate=False)
    assert len(saved["rows"]) == 5
    expected_members = {SYMBOLS[0], SYMBOLS[2]} if pin_filter else set(SYMBOLS)
    for entry in saved["rows"]:
        factor = store.get("factors", entry["factor_run_id"])
        provenance = factor["metadata"]["provenance"]
        assert provenance.get("industry_version") == expected_score_version
        assert provenance.get("universe_filters", {}).get("industry_version") == filter_version
        assert {row["instrument"] for row in factor["history"]} == expected_members
        expected_labels = {"I0", "I1"} if pin_scoring else {"KEEP"} if pin_filter else {None}
        assert {row.get("industry_code") for row in factor["history"]} == expected_labels


def test_unfrozen_research_preserves_legacy_filter_industry_display():
    requested = {"mode": "off", "industry_version": "a" * 64}
    filters = {"industries": ["KEEP"], "industry_version": "b" * 64}
    normalized = pool_industry_display(
        {"filters": filters}, {"neutralization": requested}, parse_neutralization(requested)
    )
    assert normalized == {"neutralization": {"mode": "off", "industry_version": "b" * 64}}
