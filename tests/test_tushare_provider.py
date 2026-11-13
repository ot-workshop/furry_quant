"""Offline tests for the bounded Tushare adapter."""

from __future__ import annotations

import pandas as pd
import pytest
import requests

from finance_quant.data.providers import (
    TushareConfigError,
    TushareProvider,
    TushareProviderError,
    TushareSettings,
    normalize_adj_factor,
    normalize_calendar,
    normalize_daily,
    normalize_daily_basic,
    normalize_index_daily,
)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def query(self, api_name, fields="", **params):
        self.calls.append((api_name, fields, params))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _settings(token="test-token", url="https://api.tushare.pro"):
    return TushareSettings(token=token, http_url=url)


def test_official_transport_posts_root_without_sdk_proxy_parameters(monkeypatch):

    calls = []

    class Response:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"code": 0, "data": {"fields": ["trade_date", "adj_factor"], "items": [["20230301", 2.0]]}}

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr(requests, "post", post)
    frame = TushareProvider(_settings(), min_interval=0).query("adj_factor", ts_code="000776.SZ")
    assert len(frame) == 1
    assert calls[0][0] == "https://api.tushare.pro"
    assert calls[0][1]["allow_redirects"] is False
    assert calls[0][1]["json"]["params"] == {"ts_code": "000776.SZ"}


def test_official_rejection_is_classified_without_exposing_credentials(monkeypatch):

    class Response:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"code": -1, "msg": "权限不足 secret-provider-body"}

    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: Response())
    with pytest.raises(TushareProviderError) as info:
        TushareProvider(_settings(), min_interval=0).query("daily")
    assert info.value.category == "access_denied"
    assert "secret" not in str(info.value)


def _daily_row(**overrides):
    row = {
        "ts_code": "600000.SH",
        "trade_date": "20240102",
        "open": 10.0,
        "high": 11.0,
        "low": 9.5,
        "close": 10.5,
        "pre_close": 10.0,
        "vol": 1234.0,
        "amount": 5678.0,
        "pct_chg": 5.0,
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_settings_hide_token_in_repr(monkeypatch, tmp_path):
    monkeypatch.setenv("TUSHARE_TOKEN", "super-secret-token")
    monkeypatch.setenv("TUSHARE_HTTP_URL", "https://proxy.example/tushare")
    settings = TushareSettings.from_env(env_file=tmp_path / "missing.env")

    assert settings.token == "super-secret-token"
    assert settings.http_url == "https://proxy.example/tushare"
    assert "super-secret-token" not in repr(settings)


def test_from_env_process_environment_wins(monkeypatch, tmp_path):
    env_file = tmp_path / "test.env"
    env_file.write_text(
        "TUSHARE_TOKEN=file-token\nTUSHARE_HTTP_URL=https://file.example\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    monkeypatch.delenv("TUSHARE_HTTP_URL", raising=False)

    from_file = TushareSettings.from_env(env_file=env_file)
    assert from_file.token == "file-token"
    assert from_file.http_url == "https://file.example"

    monkeypatch.setenv("TUSHARE_TOKEN", "env-token")
    from_env = TushareSettings.from_env(env_file=env_file)
    assert from_env.token == "env-token"


def test_missing_token_is_a_clear_config_error(monkeypatch, tmp_path):
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    with pytest.raises(TushareProviderError, match="TUSHARE_TOKEN"):
        TushareSettings.from_env(env_file=tmp_path / "missing.env")


@pytest.mark.parametrize(
    "url",
    [
        "http://api.tushare.pro",
        "https://user:pass@api.tushare.pro",
        "https://api.tushare.pro?token=leak",
        "https://api.tushare.pro#fragment",
        "https:///no-host",
    ],
)
def test_unsafe_urls_are_rejected(url):
    with pytest.raises(TushareConfigError) as excinfo:
        TushareSettings(token="token", http_url=url)
    assert "user:pass" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# Provider / query
# ---------------------------------------------------------------------------


def test_query_injects_client_and_forwards_arguments():
    raw = pd.DataFrame({"ts_code": ["600000.SH"]})
    client = FakeClient([raw])
    provider = TushareProvider(_settings(), min_interval=0, client=client)

    result = provider.query("daily", fields="ts_code,trade_date", ts_code="600000.SH")

    assert result is raw
    assert client.calls == [("daily", "ts_code,trade_date", {"ts_code": "600000.SH"})]


def test_query_error_does_not_leak_token_or_parameters():
    client = FakeClient([RuntimeError("token=test-token body={'x': 1}")])
    provider = TushareProvider(_settings(), min_interval=0, client=client)

    with pytest.raises(TushareProviderError) as excinfo:
        provider.query("daily", ts_code="600000.SH")

    message = str(excinfo.value)
    assert "test-token" not in message
    assert "600000.SH" not in message
    assert "{'x': 1}" not in message


def test_query_retries_transient_errors(monkeypatch):
    sleeps = []
    monkeypatch.setattr("time.sleep", lambda seconds: sleeps.append(seconds))
    client = FakeClient([TimeoutError("timeout"), Exception("请求异常"), pd.DataFrame({"ok": [1]})])
    provider = TushareProvider(_settings(), min_interval=0, client=client)

    result = provider.query("daily")

    assert list(result["ok"]) == [1]
    assert len(client.calls) == 3
    assert len(sleeps) == 2


def test_query_does_not_retry_parameter_errors():
    client = FakeClient([ValueError("invalid params")])
    provider = TushareProvider(_settings(), min_interval=0, client=client)

    with pytest.raises(TushareProviderError):
        provider.query("daily", ts_code="bad")
    assert len(client.calls) == 1


def test_query_stops_after_two_retries(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    client = FakeClient([ConnectionError("down")] * 5)
    provider = TushareProvider(_settings(), min_interval=0, client=client)

    with pytest.raises(TushareProviderError):
        provider.query("daily")
    assert len(client.calls) == 3


def test_query_returns_empty_dataframe_for_empty_response():
    client = FakeClient([pd.DataFrame(), None])
    provider = TushareProvider(_settings(), min_interval=0, client=client)

    assert provider.query("daily").empty
    assert provider.query("daily").empty


def test_query_rejects_invalid_api_name():
    client = FakeClient([])
    provider = TushareProvider(_settings(), min_interval=0, client=client)

    with pytest.raises(TushareProviderError, match="API name"):
        provider.query("../secret", ts_code="x")
    assert client.calls == []


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def test_normalize_daily_units_dates_and_codes():
    result = normalize_daily(pd.DataFrame([_daily_row()]))

    row = result.iloc[0]
    assert row["instrument"] == "SH600000"
    assert row["date"] == pd.Timestamp("2024-01-02")
    assert row["date"].tzinfo is None
    assert row["open"] == 10.0
    assert row["high"] == 11.0
    assert row["low"] == 9.5
    assert row["close"] == 10.5
    assert row["pre_close"] == 10.0
    assert row["volume"] == pytest.approx(1234.0 * 100)
    assert row["amount"] == pytest.approx(5678.0 * 1000)
    assert row["change"] == pytest.approx(0.05)


def test_normalize_daily_rejects_duplicates():
    frame = pd.DataFrame([_daily_row(), _daily_row(close=10.6)])
    with pytest.raises(TushareProviderError, match="duplicate"):
        normalize_daily(frame)


@pytest.mark.parametrize(
    "column,value",
    [
        ("open", 0.0),
        ("high", -1.0),
        ("low", float("inf")),
        ("close", float("nan")),
        ("pre_close", "not-a-number"),
    ],
)
def test_normalize_daily_rejects_invalid_prices(column, value):
    frame = pd.DataFrame([_daily_row(**{column: value})])
    with pytest.raises(TushareProviderError):
        normalize_daily(frame)


def test_normalize_daily_rejects_missing_columns():
    frame = pd.DataFrame([_daily_row()]).drop(columns=["amount"])
    with pytest.raises(TushareProviderError, match="amount"):
        normalize_daily(frame)


@pytest.mark.parametrize("code", ["600000", "600000.XX", "ABCDEF.SH", ""])
def test_normalize_daily_rejects_invalid_codes(code):
    frame = pd.DataFrame([_daily_row(ts_code=code)])
    with pytest.raises(TushareProviderError, match="ts_code"):
        normalize_daily(frame)


@pytest.mark.parametrize("date", ["20240132", "2024-01-02", "not-a-date"])
def test_normalize_daily_rejects_invalid_dates(date):
    frame = pd.DataFrame([_daily_row(trade_date=date)])
    with pytest.raises(TushareProviderError, match="date"):
        normalize_daily(frame)


def test_normalize_empty_input_returns_typed_empty_frame():
    result = normalize_daily(pd.DataFrame())
    assert result.empty
    assert list(result.columns) == [
        "instrument",
        "date",
        "open",
        "high",
        "low",
        "close",
        "pre_close",
        "volume",
        "amount",
        "change",
    ]


def test_normalize_adj_factor_preserves_raw_factor():
    raw = pd.DataFrame(
        {
            "ts_code": ["000001.SZ"],
            "trade_date": ["20240102"],
            "adj_factor": [1.25],
        }
    )
    result = normalize_adj_factor(raw)

    assert result.iloc[0]["instrument"] == "SZ000001"
    assert result.iloc[0]["date"] == pd.Timestamp("2024-01-02")
    assert result.iloc[0]["adj_factor"] == 1.25


def test_normalize_calendar_sorts_and_converts_flags():
    raw = pd.DataFrame(
        {
            "exchange": ["SSE", "SSE"],
            "cal_date": ["20240102", "20240101"],
            "is_open": [1, 0],
        }
    )
    result = normalize_calendar(raw)

    assert list(result["date"]) == [
        pd.Timestamp("2024-01-01"),
        pd.Timestamp("2024-01-02"),
    ]
    assert list(result["is_open"]) == [False, True]
    assert result["is_open"].dtype == bool


def test_normalize_calendar_rejects_non_boolean_flags():
    raw = pd.DataFrame({"cal_date": ["20240102"], "is_open": [2]})
    with pytest.raises(TushareProviderError, match="is_open"):
        normalize_calendar(raw)


def test_normalize_index_daily_uses_standard_shares_and_yuan():
    raw = pd.DataFrame(
        {
            "ts_code": ["000001.SH"],
            "trade_date": ["20240102"],
            "open": [3000.0],
            "high": [3010.0],
            "low": [2990.0],
            "close": [3005.0],
            "pre_close": [2995.0],
            "vol": [999.0],
            "amount": [1234.5],
            "pct_chg": [1.5],
        }
    )
    result = normalize_index_daily(raw)

    row = result.iloc[0]
    assert row["instrument"] == "SH000001"
    assert row["volume"] == 999.0 * 100
    assert row["amount"] == 1234.5 * 1000
    assert row["change"] == pytest.approx(0.015)


def test_normalize_index_daily_sorts_official_descending_response():
    raw = pd.DataFrame(
        {
            "ts_code": ["000300.SH", "000300.SH"],
            "trade_date": ["20240103", "20240102"],
            "open": [3001.0, 3000.0],
            "high": [3011.0, 3010.0],
            "low": [2991.0, 2990.0],
            "close": [3006.0, 3005.0],
            "pre_close": [3005.0, 2995.0],
            "vol": [1000.0, 999.0],
            "amount": [1235.0, 1234.5],
            "pct_chg": [0.03, 0.33],
        }
    )

    result = normalize_index_daily(raw)

    assert result["date"].tolist() == [pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-03")]


def test_normalize_daily_basic_preserves_missing_valuations():
    raw = pd.DataFrame(
        {
            "ts_code": ["600000.SH", "000001.SZ"],
            "trade_date": ["20240102", "20240102"],
            "pe_ttm": [10.0, None],
            "pb": [1.1, float("nan")],
            "total_mv": [100.0, 200.0],
        }
    )
    result = normalize_daily_basic(raw)

    assert result.loc[0, "pe_ttm"] == 10.0
    assert pd.isna(result.loc[1, "pe_ttm"])
    assert pd.isna(result.loc[1, "pb"])
    assert result.loc[1, "total_mv"] == 200.0 * 10000


def test_normalize_daily_basic_tolerates_missing_valuation_columns():
    raw = pd.DataFrame({"ts_code": ["600000.SH"], "trade_date": ["20240102"]})
    result = normalize_daily_basic(raw)

    assert list(result.columns) == ["instrument", "date", "pe_ttm", "pb", "total_mv"]
    assert result[["pe_ttm", "pb", "total_mv"]].isna().all().all()


def test_rate_limit_retries_are_bounded_and_permission_errors_do_not_retry(monkeypatch):

    waits = []
    monkeypatch.setattr("finance_quant.data.providers.tushare.time.sleep", waits.append)

    class Client:
        def __init__(self, message):
            self.message, self.calls = message, 0

        def query(self, *args, **kwargs):
            self.calls += 1
            raise Exception(self.message)

    limited = Client("超过每分钟频率限制 secret-marker")
    with pytest.raises(TushareProviderError) as exc:
        TushareProvider(TushareSettings("secret-marker"), min_interval=0, client=limited).query("daily")
    assert limited.calls == 3 and waits == [5, 10]
    assert exc.value.category == "rate_limit" and "secret-marker" not in str(exc.value)
    denied = Client("权限不足 secret-marker")
    with pytest.raises(TushareProviderError) as exc:
        TushareProvider(TushareSettings("secret-marker"), min_interval=0, client=denied).query("daily")
    assert denied.calls == 1 and exc.value.category == "access_denied"
