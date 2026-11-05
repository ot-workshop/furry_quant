"""Offline tests for the isolated BaoStock adapter and worker."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

import finance_quant.data.providers.baostock as baostock_module
import finance_quant.data.providers.baostock_worker as worker_module
from finance_quant.data.providers.baostock import (
    FIELDS,
    SCHEMA,
    BaoStockBundle,
    BaoStockProvider,
    BaoStockProviderError,
)

RAW_ROWS = [
    {
        "date": "2024-01-02",
        "code": "sh.600000",
        "open": "10.00",
        "high": "10.50",
        "low": "9.90",
        "close": "10.40",
        "preclose": "10.00",
        "volume": "1000",
        "amount": "10400",
        "adjustflag": "3",
        "pctChg": "4.0",
        "tradestatus": "1",
        "isST": "0",
    },
    {
        "date": "2024-01-03",
        "code": "sh.600000",
        "open": "10.40",
        "high": "10.60",
        "low": "10.20",
        "close": "10.50",
        "preclose": "10.40",
        "volume": "1100",
        "amount": "11550",
        "adjustflag": "3",
        "pctChg": "0.9615",
        "tradestatus": "1",
        "isST": "0",
    },
]

ADJ_ROWS = [{**row, "adjustflag": "1", "close": str(float(row["close"]) * 2)} for row in RAW_ROWS]


class RecordingWorker:
    def __init__(self, payload=None):
        self.calls = []
        self.payload = payload
        self.error = None

    def __call__(self, request):
        self.calls.append(dict(request))
        if self.error is not None:
            raise self.error
        if self.payload is not None:
            return self.payload
        return {"raw": RAW_ROWS, "adjusted": ADJ_ROWS}


def _payload():
    return {"raw": RAW_ROWS, "adjusted": ADJ_ROWS}


def test_network_is_serial_across_different_cache_roots(tmp_path):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    active = 0
    maximum = 0
    guard = threading.Lock()

    def worker(request):
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(active, maximum)
        time.sleep(0.05)
        with guard:
            active -= 1
        return _payload()

    providers = [BaoStockProvider(tmp_path / str(n), worker=worker) for n in range(2)]
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda p: p.fetch("600000.SH", "20240101", "20240103"), providers))
    assert maximum == 1 and len(results) == 2


def _provider(tmp_path, worker, **kwargs):
    return BaoStockProvider(tmp_path, worker=worker, **kwargs)


def _hashes(directory):
    return {
        name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
        for name in ("raw.parquet", "adjusted.parquet")
    }


# ---------------------------------------------------------------------------
# Provider and cache
# ---------------------------------------------------------------------------


def test_success_returns_original_tables_and_reuses_cache(tmp_path):
    worker = RecordingWorker()
    provider = _provider(tmp_path, worker)

    bundle = provider.fetch("600000.SH", "20240101", "2024-01-03")

    assert isinstance(bundle, BaoStockBundle)
    assert worker.calls == [{"code": "600000.SH", "start": "2024-01-01", "end": "2024-01-03"}]
    assert list(bundle.raw.columns) == list(FIELDS)
    assert list(bundle.adjusted.columns) == list(FIELDS)
    assert bundle.raw.to_dict("records") == RAW_ROWS
    assert bundle.adjusted["adjustflag"].tolist() == ["1", "1"]

    metadata = bundle.metadata
    assert metadata["source"] == "baostock"
    assert metadata["schema"] == SCHEMA
    assert metadata["request"] == worker.calls[0]
    assert metadata["cache_key"] == metadata["cache_key"].strip()
    assert metadata["cache_path"] == f"baostock/{metadata['cache_key']}"
    assert metadata["files"] == _hashes(tmp_path / metadata["cache_path"])

    cached = provider.fetch("600000.SH", "2024-01-01", "2024-01-03")
    assert len(worker.calls) == 1
    assert cached.metadata == metadata
    assert cached.raw.equals(bundle.raw)
    assert cached.adjusted.equals(bundle.adjusted)
    assert "cache_hit" not in cached.metadata


def test_checksum_corruption_fails_closed_without_overwriting(tmp_path):
    worker = RecordingWorker()
    provider = _provider(tmp_path, worker)
    bundle = provider.fetch("600000.SH", "20240101", "20240103")
    raw_path = tmp_path / bundle.metadata["cache_path"] / "raw.parquet"
    raw_path.write_bytes(b"corrupted")

    with pytest.raises(BaoStockProviderError, match="corrupt"):
        provider.fetch("600000.SH", "20240101", "20240103")

    assert len(worker.calls) == 1
    assert raw_path.read_bytes() == b"corrupted"


def test_namespace_isolates_cache_entries(tmp_path):
    worker = RecordingWorker()
    first = BaoStockProvider(tmp_path, namespace="generation-a", worker=worker)
    second = BaoStockProvider(tmp_path, namespace="generation-b", worker=worker)

    one = first.fetch("600000.SH", "20240101", "20240103")
    two = second.fetch("600000.SH", "20240101", "20240103")
    assert len(worker.calls) == 2
    assert one.metadata["cache_key"] != two.metadata["cache_key"]
    assert one.metadata["cache_path"] != two.metadata["cache_path"]

    one_again = first.fetch("600000.SH", "20240101", "20240103")
    assert len(worker.calls) == 2
    assert one_again.metadata == one.metadata


def test_constructor_does_not_create_cache_directories(tmp_path):
    root = tmp_path / "not-created"
    BaoStockProvider(root)
    assert not root.exists()


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        "600000",
        "600000.sh",
        "600000.BJ",
        "600000.HK",
        "60000.SH",
        "6000000.SH",
        " 600000.SH",
        "600000.SH ",
        "600000.SH\n",
        None,
    ],
)
def test_strict_code_validation(tmp_path, code):
    worker = RecordingWorker()
    with pytest.raises(BaoStockProviderError, match="canonical"):
        _provider(tmp_path, worker).fetch(code, "20240101", "20240103")
    assert worker.calls == []


@pytest.mark.parametrize(
    "start,end",
    [
        ("2024-1-01", "20240103"),
        ("2024010", "20240103"),
        ("20240230", "20240301"),
        ("2024/01/01", "20240103"),
        ("20240103", "20240101"),
        (20240101, "20240103"),
    ],
)
def test_strict_date_validation(tmp_path, start, end):
    worker = RecordingWorker()
    with pytest.raises(BaoStockProviderError):
        _provider(tmp_path, worker).fetch("600000.SH", start, end)
    assert worker.calls == []


def test_injected_timeout_is_sanitized(tmp_path):
    worker = RecordingWorker()
    worker.error = TimeoutError("secret socket detail")
    with pytest.raises(BaoStockProviderError, match="timed out") as excinfo:
        _provider(tmp_path, worker).fetch("600000.SH", "20240101", "20240103")
    assert "secret" not in str(excinfo.value)
    assert len(worker.calls) == 2


def test_transient_worker_failure_reconnects_once_and_then_reuses_cache(tmp_path):
    calls = []

    def flaky(request):
        calls.append(request)
        if len(calls) == 1:
            raise TimeoutError("private socket detail")
        return _payload()

    provider = _provider(tmp_path, flaky)
    result = provider.fetch("600000.SH", "20240101", "20240103")
    assert len(result.raw) == 2 and len(calls) == 2
    assert provider.fetch("600000.SH", "20240101", "20240103").metadata == result.metadata
    assert len(calls) == 2


def test_invalid_worker_content_is_not_retried(tmp_path):
    worker = RecordingWorker(payload={"raw": [], "adjusted": ADJ_ROWS})
    with pytest.raises(BaoStockProviderError, match="invalid data"):
        _provider(tmp_path, worker).fetch("600000.SH", "20240101", "20240103")
    assert len(worker.calls) == 1


# ---------------------------------------------------------------------------
# Payload validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"raw": [], "adjusted": ADJ_ROWS},
        {"raw": RAW_ROWS, "adjusted": []},
        {"raw": RAW_ROWS},
        {"raw": RAW_ROWS, "adjusted": ADJ_ROWS, "extra": []},
        {"raw": "not-a-list", "adjusted": ADJ_ROWS},
        {"raw": [1], "adjusted": ADJ_ROWS},
        {"raw": [{key: value for key, value in RAW_ROWS[0].items() if key != "close"}], "adjusted": ADJ_ROWS},
    ],
)
def test_malformed_worker_payload_is_rejected(tmp_path, payload):
    worker = RecordingWorker(payload=payload)
    with pytest.raises(BaoStockProviderError):
        _provider(tmp_path, worker).fetch("600000.SH", "20240101", "20240103")


# ---------------------------------------------------------------------------
# Default subprocess worker
# ---------------------------------------------------------------------------


def test_default_subprocess_worker_invokes_module_with_timeout(tmp_path, monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(_payload()),
            stderr="",
        )

    monkeypatch.setattr(baostock_module.subprocess, "run", fake_run)
    bundle = BaoStockProvider(tmp_path, timeout=7.5).fetch("600000.SH", "20240101", "20240103")

    assert captured["command"] == [
        baostock_module.sys.executable,
        "-m",
        "finance_quant.data.providers.baostock_worker",
    ]
    assert json.loads(captured["input"]) == {
        "code": "600000.SH",
        "start": "2024-01-01",
        "end": "2024-01-03",
    }
    assert captured["timeout"] == 7.5
    assert captured["text"] is True
    assert captured["capture_output"] is True
    assert captured["shell"] is False
    assert len(bundle.raw) == 2


def test_default_subprocess_timeout_is_sanitized(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        raise baostock_module.subprocess.TimeoutExpired(command, kwargs["timeout"], output="secret stdout")

    monkeypatch.setattr(baostock_module.subprocess, "run", fake_run)
    with pytest.raises(BaoStockProviderError, match="timed out") as excinfo:
        BaoStockProvider(tmp_path, timeout=1).fetch("600000.SH", "20240101", "20240103")
    assert "secret" not in str(excinfo.value)


def test_default_subprocess_nonzero_is_sanitized(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        return SimpleNamespace(
            returncode=1,
            stdout='{"error":"secret response"}',
            stderr="secret stderr body",
        )

    monkeypatch.setattr(baostock_module.subprocess, "run", fake_run)
    with pytest.raises(BaoStockProviderError) as excinfo:
        BaoStockProvider(tmp_path).fetch("600000.SH", "20240101", "20240103")
    message = str(excinfo.value)
    assert "secret" not in message
    assert "stderr" not in message


def test_default_subprocess_invalid_json_is_rejected(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        return SimpleNamespace(returncode=0, stdout="not-json", stderr="")

    monkeypatch.setattr(baostock_module.subprocess, "run", fake_run)
    with pytest.raises(BaoStockProviderError):
        BaoStockProvider(tmp_path).fetch("600000.SH", "20240101", "20240103")


# ---------------------------------------------------------------------------
# Worker internals with a fake SDK
# ---------------------------------------------------------------------------


class FakeResult:
    def __init__(self, rows, fields=FIELDS, error_code="0"):
        self.rows = list(rows)
        self.fields = list(fields)
        self.error_code = error_code
        self.index = 0

    def next(self):
        if self.index < len(self.rows):
            self.index += 1
            return True
        return False

    def get_row_data(self):
        return self.rows[self.index - 1]


class FakeSDK:
    def __init__(self, raw=None, adjusted=None, print_messages=False):
        self.raw = list(RAW_ROWS if raw is None else raw)
        self.adjusted = list(ADJ_ROWS if adjusted is None else adjusted)
        self.calls = []
        self.print_messages = print_messages

    def login(self):
        self.calls.append(("login",))
        if self.print_messages:
            print("login success!")
        return FakeResult([])

    def logout(self):
        self.calls.append(("logout",))

    def query_history_k_data_plus(self, code, fields, start_date=None, end_date=None, frequency=None, adjustflag=None):
        self.calls.append(
            {
                "code": code,
                "fields": fields,
                "start_date": start_date,
                "end_date": end_date,
                "frequency": frequency,
                "adjustflag": adjustflag,
            }
        )
        if self.print_messages:
            print("query noise")
        rows = self.raw if adjustflag == "3" else self.adjusted
        return FakeResult([[row[field] for field in FIELDS] for row in rows])


def _request():
    return {"code": "600000.SH", "start": "2024-01-01", "end": "2024-01-03"}


def test_worker_queries_raw_then_adjusted_and_logs_out(capsys):
    sdk = FakeSDK(print_messages=True)

    payload = worker_module.fetch_history(_request(), sdk=sdk)

    assert [call[0] if isinstance(call, tuple) else "query" for call in sdk.calls] == [
        "login",
        "query",
        "query",
        "logout",
    ]
    queries = [call for call in sdk.calls if isinstance(call, dict)]
    assert [query["adjustflag"] for query in queries] == ["3", "1"]
    for query in queries:
        assert query["code"] == "sh.600000"
        assert query["fields"] == ",".join(FIELDS)
        assert query["frequency"] == "d"
        assert query["start_date"] == "2024-01-01"
        assert query["end_date"] == "2024-01-03"
    assert payload["raw"][0]["close"] == "10.40"
    assert payload["adjusted"][0]["adjustflag"] == "1"
    assert capsys.readouterr().out == ""


def test_worker_logs_out_after_query_failure():
    sdk = FakeSDK(raw=[])  # empty result -> failure

    with pytest.raises(worker_module.WorkerError):
        worker_module.fetch_history(_request(), sdk=sdk)

    assert sdk.calls[-1] == ("logout",)


def test_worker_rejects_nonzero_error_after_iteration():
    class FailingResult(FakeResult):
        def next(self):
            self.error_code = "1"
            return False

    class FailingSDK(FakeSDK):
        def query_history_k_data_plus(self, *args, **kwargs):
            self.calls.append(kwargs)
            return FailingResult([])

    with pytest.raises(worker_module.WorkerError):
        worker_module.fetch_history(_request(), sdk=FailingSDK())
