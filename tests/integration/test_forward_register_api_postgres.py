"""The manual POST and GET route share the real PostgreSQL registration proof."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
from threading import Event

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, text
from test_forward_score_postgres import CODES
from test_postgres_runtime import engine as engine

from finance_quant.api import create_app
from finance_quant.application.tasks import TaskService
from finance_quant.data.managed import ManagedFetcher
from finance_quant.data.market import write_market_snapshot_files
from finance_quant.persistence.forward import SHANGHAI, registration_row_confirmed
from finance_quant.persistence.maintenance import write_gate
from finance_quant.persistence.market import MarketRepository
from finance_quant.persistence.results import PgResultStore
from finance_quant.persistence.schema import dataset, forward_attempt, forward_registration, research_task
from finance_quant.persistence.tasks import PgTaskStore
from finance_quant.web import forward_writes

pytestmark = pytest.mark.skipif(
    os.environ.get("FINANCE_QUANT_TEST_POSTGRES") != "1", reason="requires isolated PostgreSQL"
)
URL = "/api/v1/forward-registrations"
KEY = {"idempotency-key": "api-registration-fixture"}


def _client(engine, root):
    runs = root / "runs"
    service = TaskService(runs, root / "data", task_store=PgTaskStore(runs, engine=engine))
    store = PgResultStore(runs, engine=engine, storage_root=root)
    return TestClient(create_app(result_store=store, task_service=service), base_url="http://127.0.0.1")


@pytest.fixture
def setup_registration(engine, tmp_path, monkeypatch):
    monkeypatch.delenv("ALLOWED_ORIGINS", raising=False)
    with engine.connect() as connection:
        today = connection.scalar(text("select (clock_timestamp() at time zone 'Asia/Shanghai')::date"))
    signal = today + timedelta(days=1)
    sessions = [signal + timedelta(days=offset) for offset in (-7, -5, -1, 0, 2, 4)]
    days = pd.date_range(sessions[0], sessions[-1])
    calendar = pd.DataFrame({"date": days, "is_open": days.isin(pd.DatetimeIndex(sessions))})

    def source(frame, *, name="fixed-source", provider="a" * 64, manifest_name=None, coverage=None):
        leaf = tmp_path / "data" / "research" / "subsets" / name
        leaf.mkdir(parents=True)
        quotes = pd.DataFrame({"fixture": [1]})
        files = write_market_snapshot_files(leaf, quotes, quotes, frame)
        manifest = {"id": manifest_name or name, "schema_version": "market-v2", "provider_id": provider, "files": files}
        if coverage is not None:
            manifest.update(dict(zip(("start", "end"), coverage, strict=True)))
        (leaf / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        record = MarketRepository(engine=engine).register_snapshot(leaf, tmp_path, request={})
        return record["id"], leaf

    identifier, leaf = source(calendar)
    document = {
        "name": "  Manual forward fixture ",
        "signal_date": signal.isoformat(),
        "pool_codes": [" 600001.sh ", "000001.SZ", "600000.SH"],
        "top_k": 1,
        "factor_window": 2,
        "factor_direction": "high",
        "dataset_id": identifier,
    }
    return _client(engine, tmp_path), document, signal, sessions, calendar, source, leaf


def _rows(engine, table):
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(select(table).order_by(table.c.id)).mappings()]


def test_source_options_project_only_ready_managed_metadata(engine, setup_registration, monkeypatch):
    client, document, _, sessions, calendar, source, _ = setup_registration
    start, end = sessions[0].isoformat(), sessions[-1].isoformat()
    continued, _ = source(calendar, name="ready", coverage=(start, end))
    rejected, rejected_path = source(calendar, name="removed")
    (rejected_path / "rejected.json").write_text("{}", encoding="utf-8")
    failed, _ = source(calendar, name="not-ready")
    _, mismatched_path = source(calendar, name="wrong-digest")
    (mismatched_path / "manifest.json").write_text("{}", encoding="utf-8")
    source(calendar, name="not-managed", provider="external")
    with engine.begin() as connection:
        connection.execute(dataset.update().where(dataset.c.id == failed).values(status="failed"))
    before = _rows(engine, dataset)

    def unexpected(*args, **kwargs):
        pytest.fail("Source options must not hash parquet files or invoke the queue")

    monkeypatch.setattr(forward_writes, "snapshot_dataset_record", unexpected)
    monkeypatch.setattr(pd, "read_parquet", unexpected)
    monkeypatch.setattr(PgTaskStore, "create", unexpected)
    result = client.get(f"{URL}/sources")
    assert result.status_code == 200, result.text
    assert result.json() == {
        "items": [
            {"id": continued, "name": "ready", "start": start, "end": end},
            {"id": document["dataset_id"], "name": "fixed-source", "start": None, "end": None},
        ],
        "has_more": False,
    }
    assert all(rejected not in item.values() for item in result.json()["items"])
    assert client.get(f"{URL}/sources?limit=26").status_code == 422
    scanned = [client.get(f"{URL}/sources?offset={offset}&limit=1").json() for offset in range(5)]
    assert [item for page in scanned for item in page["items"]] == result.json()["items"]
    assert [page["has_more"] for page in scanned] == [True, True, True, True, False]
    assert _rows(engine, dataset) == before
    assert _rows(engine, forward_registration) == _rows(engine, forward_attempt) == _rows(engine, research_task) == []


def test_post_freezes_managed_dates_and_replays_when_source_is_gone(engine, setup_registration, monkeypatch):
    client, document, _, sessions, _, _, leaf = setup_registration

    def unexpected(*args, **kwargs):
        pytest.fail("Registration invoked the queue or external acquisition")

    monkeypatch.setattr(ManagedFetcher, "request", unexpected)
    monkeypatch.setattr(PgTaskStore, "claim", unexpected)
    monkeypatch.setattr(PgTaskStore, "create", unexpected)
    sources = _rows(engine, dataset)
    response = client.post(URL, json=document, headers=KEY)
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert set(receipt) == {"registration_id", "confirmed_at"}
    identifier = receipt["registration_id"]
    (saved,) = _rows(engine, forward_registration)
    assert registration_row_confirmed(saved)
    assert saved["entry_date"] == sessions[-2] and saved["exit_date"] == sessions[-1]
    assert saved["pool_codes"] == CODES and saved["pool_size"] == 3
    assert saved["data_rules"]["calendar"]["trading_dates"] == [day.isoformat() for day in sessions]
    detail = client.get(f"{URL}/{identifier}").json()
    assert detail["id"] == identifier and detail["confirmed_at"] == receipt["confirmed_at"]
    assert detail["state"] == "awaiting_score" and detail["versions"]["source_dataset"] == document["dataset_id"]
    assert detail["entry_date"] == sessions[-2].isoformat() and detail["exit_date"] == sessions[-1].isoformat()

    (leaf / "manifest.json").unlink()
    replay = client.post(URL, json=dict(document, name=document["name"].strip(), pool_codes=CODES), headers=KEY)
    assert replay.status_code == 200 and replay.json() == receipt
    for change in ({"top_k": 2}, {"dataset_id": "snapshot-" + "f" * 64}):
        conflict = client.post(URL, json=dict(document, **change), headers=KEY)
        assert conflict.status_code == 409 and "参数已改变" in conflict.json()["detail"]
    assert _rows(engine, forward_registration) == [saved]
    assert _rows(engine, dataset) == sources
    assert _rows(engine, forward_attempt) == _rows(engine, research_task) == []


@pytest.fixture
def two_connection_engine(engine):
    with engine.connect() as connection:
        schema = connection.scalar(text("select current_schema()"))
    bounded = create_engine(
        engine.url, pool_size=2, max_overflow=0, pool_timeout=4, connect_args={"options": f"-c search_path={schema}"}
    )
    try:
        yield bounded
    finally:
        bounded.dispose()


def test_concurrent_same_key_completes_with_only_two_pool_connections(
    engine, tmp_path, setup_registration, two_connection_engine
):
    _, document, _, _, _, _, _ = setup_registration
    client = _client(two_connection_engine, tmp_path)
    with ThreadPoolExecutor(max_workers=6) as workers:
        requests = [workers.submit(client.post, URL, json=document, headers=KEY) for _ in range(6)]
        replies = [result.result(timeout=20) for result in requests]
    assert all(response.status_code == 200 for response in replies), [response.text for response in replies]
    assert all(response.json() == replies[0].json() for response in replies)
    (saved,) = _rows(engine, forward_registration)
    assert registration_row_confirmed(saved)
    assert _rows(engine, forward_attempt) == _rows(engine, research_task) == []


@pytest.mark.parametrize("scenario", ["same-request", "changed-request", "no-winner"])
def test_post_rechecks_same_key_when_source_disappears_mid_request(
    engine, tmp_path, setup_registration, two_connection_engine, monkeypatch, scenario
):
    _, document, _, _, _, _, leaf = setup_registration
    client = _client(two_connection_engine, tmp_path)
    first_miss, remove_source = Event(), Event()
    sessions = []
    original = forward_writes._source_calendar

    def pause_after_miss(connection, root, dataset_id):
        sessions.append(connection.scalar(text("select pg_backend_pid()")))
        if not first_miss.is_set():
            # A call to the source reader proves the first lookup found no registration.
            first_miss.set()
            assert remove_source.wait(timeout=15)
        return original(connection, root, dataset_id)

    monkeypatch.setattr(forward_writes, "_source_calendar", pause_after_miss)
    delayed = dict(document, top_k=2) if scenario == "changed-request" else document
    winner = None
    with ThreadPoolExecutor(max_workers=1) as workers:
        request = workers.submit(client.post, URL, json=delayed, headers=KEY)
        try:
            assert first_miss.wait(timeout=10)
            if scenario != "no-winner":
                winner = client.post(URL, json=document, headers=KEY)
                assert winner.status_code == 200, winner.text
                assert len(set(sessions)) == 2
            (leaf / "manifest.json").unlink()
        finally:
            remove_source.set()
        response = request.result(timeout=15)

    assert two_connection_engine.pool.checkedout() == 0
    # A separate engine cannot acquire this while either POST session retains a shared lock.
    with write_gate(engine, exclusive=True):
        rows = _rows(engine, forward_registration)
        assert _rows(engine, forward_attempt) == _rows(engine, research_task) == []
    if scenario == "no-winner":
        assert response.status_code == 422
        assert response.json() == {"detail": "来源数据集的受管清单或文件摘要无法验证"}
        assert rows == []
    else:
        assert winner is not None
        (saved,) = rows
        assert registration_row_confirmed(saved)
        assert saved["id"] == winner.json()["registration_id"]
        if scenario == "changed-request":
            assert response.status_code == 409 and "参数已改变" in response.json()["detail"]
        else:
            assert response.status_code == 200 and response.json() == winner.json()


@pytest.mark.parametrize("phase", ["before", "at-seal", "at-stage"])
def test_actual_post_uses_both_strict_shanghai_deadline_samples(engine, setup_registration, phase):
    client, document, signal, _, _, _, _ = setup_registration
    signal -= timedelta(days=1)
    document["signal_date"] = signal.isoformat()
    deadline = datetime.combine(signal, time.min, tzinfo=SHANGHAI)
    early = deadline - timedelta(microseconds=2)
    samples = iter(
        {"before": [early, deadline - timedelta(microseconds=1)], "at-seal": [early, deadline], "at-stage": [deadline]}[
            phase
        ]
    )

    def clock(connection, cursor, statement, parameters, context, executemany):
        if statement == "select clock_timestamp()":
            return "select cast(%s as timestamptz)", (next(samples).isoformat(),)
        return statement, parameters

    event.listen(engine, "before_cursor_execute", clock, retval=True)
    try:
        response = client.post(URL, headers=KEY, json=document)
    finally:
        event.remove(engine, "before_cursor_execute", clock)
    (row,) = _rows(engine, forward_registration)
    detail = client.get(f"{URL}/{row['id']}").json()
    if phase == "before":
        assert response.status_code == 200, response.text
        assert datetime.fromisoformat(response.json()["confirmed_at"]) == deadline - timedelta(microseconds=1)
        assert registration_row_confirmed(row) and detail["confirmed"] is True
    else:
        assert response.status_code == 409 and "00:00" in response.json()["detail"]
        assert row["status"] == detail["state"] == "expired" and row["confirmed_at"] is None
        replay = client.post(URL, headers=KEY, json=document)
        assert replay.status_code == 409
    assert len(_rows(engine, forward_registration)) == 1
    assert _rows(engine, forward_attempt) == _rows(engine, research_task) == []


def test_maintenance_blocks_post_while_get_stays_available(engine, setup_registration):
    client, document, _, _, _, _, _ = setup_registration
    with write_gate(engine, exclusive=True):
        response = client.post(URL, json=document, headers=KEY)
        assert response.status_code == 409 and "维护" in response.json()["detail"]
        assert client.get(URL).status_code == 200
    assert _rows(engine, forward_registration) == _rows(engine, research_task) == []
    assert client.post(URL, json=document, headers=KEY).status_code == 200


@pytest.mark.parametrize(
    ("damage", "reason"),
    [
        ("unknown", "不存在"),
        ("corrupt", "摘要无法验证"),
        ("provider", "摘要无法验证"),
        ("gap", "缺少连续日期"),
        ("closed", "信号日必须"),
        ("future", "未覆盖入场日与退出日"),
        ("warmup", "缺少动量预热交易日"),
    ],
)
def test_bad_sources_and_calendars_leave_no_registration(engine, setup_registration, damage, reason):
    client, document, signal, sessions, calendar, source, leaf = setup_registration
    if damage == "unknown":
        document["dataset_id"] = "snapshot-" + "b" * 64
    elif damage == "corrupt":
        with (leaf / "market.parquet").open("ab") as stream:
            stream.write(b"damaged-data")
    else:
        provider = "untrusted-name" if damage == "provider" else "a" * 64
        if damage == "gap":
            calendar = calendar.loc[calendar.date.ne(pd.Timestamp(signal + timedelta(days=1)))]
        elif damage == "closed":
            calendar.loc[calendar.date.eq(pd.Timestamp(signal)), "is_open"] = False
        elif damage == "future":
            calendar = calendar.loc[calendar.date.le(pd.Timestamp(sessions[-2]))]
        elif damage == "warmup":
            calendar = calendar.loc[calendar.date.ge(pd.Timestamp(signal - timedelta(days=1)))]
        document["dataset_id"] = source(calendar, name="modified-source", provider=provider)[0]
    response = client.post(URL, json=document, headers=KEY)
    assert response.status_code == 422 and reason in response.json()["detail"]
    assert _rows(engine, forward_registration) == _rows(engine, forward_attempt) == _rows(engine, research_task) == []


def test_existing_research_http_admission_and_strategy_constraints(engine, setup_registration):
    client, document, _, _, _, _, _ = setup_registration
    for headers in (
        {"host": "unconfigured.invalid"},
        {"origin": "http://unconfigured.invalid"},
        {"sec-fetch-site": "cross-site"},
    ):
        assert client.post(URL, json=document, headers={**KEY, **headers}).status_code == 403
    assert client.post(URL, content="{}", headers=KEY).status_code == 415
    json_headers = {**KEY, "content-type": "application/json"}
    assert client.post(URL, content="x" * 65537, headers=json_headers).status_code == 413
    assert client.post(URL, content='{"name":"a","name":"b"}', headers=json_headers).status_code == 422
    assert client.post(URL, json=document).status_code == 422
    for values in (
        {"entry_date": date.today().isoformat()},
        {"top_k": True},
        {"top_k": 3},
        {"factor_window": 1},
        {"factor_direction": "other"},
        {"pool_codes": ["000001.sz", "000001.SZ"]},
        {"dataset_id": "../../source"},
    ):
        assert client.post(URL, json=dict(document, **values), headers=KEY).status_code == 422
    assert _rows(engine, forward_registration) == _rows(engine, research_task) == []
