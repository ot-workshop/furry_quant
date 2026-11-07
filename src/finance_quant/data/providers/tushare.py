"""Bounded adapter for the Tushare SDK.

The SDK is imported lazily so callers can inject a client for offline use. This
module keeps credentials out of reprs and error messages, throttles requests,
retries transient network failures, and normalizes the first batch of Tushare
endpoints into plain pandas DataFrames.
"""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np
import pandas as pd

from finance_quant.data.symbols import to_qlib

DEFAULT_HTTP_URL = "https://api.tushare.pro"
MAX_RETRIES = 2

_API_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CODE_RE = re.compile(r"^\d{6}\.(?:SH|SZ|BJ)$")
_RETRY_DELAY = 0.25

_DAILY_COLUMNS = [
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
_DAILY_REQUIRED = (
    "ts_code",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "vol",
    "amount",
    "pct_chg",
)
_ADJ_COLUMNS = ["instrument", "date", "adj_factor"]
_CALENDAR_COLUMNS = ["date", "is_open"]
_INDEX_COLUMNS = [
    "instrument",
    "date",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "volume",
    "amount",
]
_INDEX_REQUIRED = (
    "ts_code",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "vol",
    "amount",
)
_BASIC_COLUMNS = ["instrument", "date", "pe_ttm", "pb", "total_mv"]

# Statement -> (Tushare API, value fields in yuan). Income and cash-flow values are
# year-to-date cumulative; balance-sheet values are period-end stocks.
FINANCIAL_STATEMENTS = {
    "income": (
        "income",
        ("total_revenue", "revenue", "oper_cost", "operate_profit", "total_profit", "n_income", "n_income_attr_p"),
    ),
    "balance": (
        "balancesheet",
        (
            "total_assets",
            "total_liab",
            "total_hldr_eqy_exc_min_int",
            "total_hldr_eqy_inc_min_int",
            "total_cur_assets",
            "total_cur_liab",
        ),
    ),
    "cashflow": ("cashflow", ("n_cashflow_act",)),
}
FINANCIAL_VALUE_FIELDS = tuple(field for _, fields in FINANCIAL_STATEMENTS.values() for field in fields)
FINANCIAL_KEY_COLUMNS = (
    "ts_code",
    "statement",
    "report_type",
    "comp_type",
    "report_period",
    "ann_date",
    "f_ann_date",
    "update_flag",
)
_FINANCIAL_RAW_KEYS = ("ts_code", "ann_date", "f_ann_date", "end_date", "report_type", "comp_type", "update_flag")


class TushareProviderError(RuntimeError):
    """Public adapter error; never carries credentials or response bodies."""

    def __init__(self, message: str, *, category: str = "provider"):
        super().__init__(message)
        self.category = category


class TushareConfigError(TushareProviderError):
    """Invalid or missing adapter configuration."""


@dataclass(frozen=True)
class TushareSettings:
    """Connection settings read from the process environment or a dotenv file."""

    token: str = field(repr=False)
    http_url: str = DEFAULT_HTTP_URL

    def __post_init__(self) -> None:
        token = "" if self.token is None else str(self.token).strip()
        http_url = DEFAULT_HTTP_URL if self.http_url is None else str(self.http_url).strip()
        if not token:
            raise TushareConfigError("TUSHARE_TOKEN is not configured")
        parts = urlsplit(http_url)
        if (
            parts.scheme.lower() != "https"
            or parts.hostname is None
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
        ):
            raise TushareConfigError("TUSHARE_HTTP_URL must be HTTPS without userinfo, query, or fragment")
        object.__setattr__(self, "token", token)
        object.__setattr__(self, "http_url", http_url)

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> TushareSettings:
        """Read settings with process environment taking priority over ``env_file``."""
        file_values: dict[str, str] = {}
        path = Path(env_file) if env_file is not None else Path(".env")
        try:
            if env_file is not None or path.is_file():
                from dotenv import dotenv_values

                file_values = {key: value for key, value in dotenv_values(path).items() if value is not None}
        except OSError:
            file_values = {}

        token = os.environ.get("TUSHARE_TOKEN") or file_values.get("TUSHARE_TOKEN") or ""
        http_url = os.environ.get("TUSHARE_HTTP_URL") or file_values.get("TUSHARE_HTTP_URL") or DEFAULT_HTTP_URL
        return cls(token=token, http_url=http_url)


def _is_retryable(exc: BaseException) -> bool:
    """Classify transient transport failures without importing requests."""
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    name = type(exc).__name__.lower()
    if any(part in name for part in ("timeout", "connection", "network", "protocol", "chunked")):
        return True
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "timeout",
            "timed out",
            "connection",
            "network",
            "请求异常",
            "temporarily unavailable",
            "remote end closed",
            "max retries",
        )
    )


def _error_category(exc: BaseException) -> str:
    if isinstance(exc, TushareProviderError):
        return exc.category
    text = str(exc).lower()
    if any(marker in text for marker in ("频率", "频次", "每分钟", "每秒", "too many", "rate limit", "too frequent")):
        return "rate_limit"
    if _is_retryable(exc):
        return "transport"
    if any(marker in text for marker in ("权限", "积分", "permission", "access denied", "invalid token")):
        return "access_denied"
    if any(marker in text for marker in ("不存在", "not found", "unknown api")):
        return "unsupported"
    return "provider"


class TushareProvider:
    """Throttled, retrying wrapper around a Tushare ``pro_api`` client."""

    def __init__(
        self,
        settings: TushareSettings,
        min_interval: float = 0.35,
        client=None,
    ) -> None:
        if min_interval < 0:
            raise TushareConfigError("min_interval must be non-negative")
        self.settings = settings
        self.min_interval = float(min_interval)
        self._client = client
        self._last_call = 0.0
        self._throttle_lock = threading.Lock()

    def _get_client(self):
        if self._client is None:
            if self.settings.http_url.rstrip("/") == DEFAULT_HTTP_URL:
                self._client = _OfficialClient(self.settings)
                return self._client
            try:
                import tushare as ts

                client = ts.pro_api(self.settings.token, timeout=20)
                client._DataApi__http_url = self.settings.http_url
            except Exception:
                raise TushareProviderError("failed to initialize the Tushare SDK client") from None
            self._client = client
        return self._client

    def _throttle(self) -> None:
        with self._throttle_lock:
            wait = self._last_call + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

    def query(self, api_name: str, *, fields: str = "", **params) -> pd.DataFrame:
        """Run one Tushare query with throttling and bounded network retries."""
        if not isinstance(api_name, str) or _API_NAME_RE.fullmatch(api_name) is None:
            raise TushareProviderError("invalid Tushare API name")

        client = self._get_client()
        retries = 0
        while True:
            self._throttle()
            try:
                raw = client.query(api_name, fields=fields, **params)
                break
            except Exception as exc:
                from finance_quant.data.network_budget import DownloadPaused

                if isinstance(exc, DownloadPaused):
                    raise
                category = _error_category(exc)
                if retries < MAX_RETRIES and category in {"transport", "rate_limit"}:
                    retries += 1
                    time.sleep((5 if category == "rate_limit" else _RETRY_DELAY) * retries)
                    continue
                raise TushareProviderError(
                    f"Tushare API request failed: {api_name}",
                    category=category,
                ) from None

        if raw is None:
            return pd.DataFrame()
        if not isinstance(raw, pd.DataFrame):
            raise TushareProviderError("Tushare returned an unexpected response type")
        return raw


class _OfficialClient:
    """Official HTTPS JSON contract, independent of SDK proxy customizations."""

    def __init__(self, settings):
        self.settings = settings

    def query(self, api_name, fields="", **params):
        import requests

        from finance_quant.data.network_budget import reserve_tushare

        reserve_tushare(api_name)
        response = requests.post(
            DEFAULT_HTTP_URL,
            json={"api_name": api_name, "token": self.settings.token, "params": params, "fields": fields},
            timeout=20,
            allow_redirects=False,
        )
        response.raise_for_status()
        if response.status_code != 200:
            raise TushareProviderError("Official endpoint returned an unexpected status")
        result = response.json()
        if result.get("code") != 0:
            # Use provider text only internally to classify the failure; never expose it.
            category = _error_category(RuntimeError(str(result.get("msg", ""))))
            raise TushareProviderError("Official API rejected the request", category=category)
        data = result["data"]
        return pd.DataFrame(data["items"], columns=data["fields"])


def _empty_frame(raw, columns: list[str]) -> pd.DataFrame | None:
    if not isinstance(raw, pd.DataFrame):
        raise TushareProviderError("Tushare response must be a pandas DataFrame")
    if raw.empty:
        return pd.DataFrame(columns=columns)
    return None


def _require_columns(raw: pd.DataFrame, required: tuple[str, ...]) -> None:
    missing = [column for column in required if column not in raw.columns]
    if missing:
        raise TushareProviderError("Tushare response is missing required column(s): " + ", ".join(missing))


def _parse_dates(values: pd.Series) -> pd.Series:
    if pd.api.types.is_datetime64_any_dtype(values):
        dates = pd.to_datetime(values, errors="coerce")
    else:
        text = values.astype("string").str.strip()
        dates = pd.to_datetime(text, format="%Y%m%d", errors="coerce")
    if dates.isna().any():
        raise TushareProviderError("Tushare response contains an invalid date")
    if dates.dt.tz is not None:
        raise TushareProviderError("Trading dates must not contain timezone or intraday timestamps")
    if not dates.equals(dates.dt.normalize()):
        raise TushareProviderError("Trading dates must not contain intraday timestamps")
    return dates


def _instruments(raw: pd.DataFrame) -> pd.Series:
    codes = raw["ts_code"].astype("string").str.strip().str.upper()
    valid = codes.map(lambda code: isinstance(code, str) and _CODE_RE.fullmatch(code) is not None)
    if not bool(valid.all()):
        raise TushareProviderError("Tushare response contains an invalid ts_code")
    return codes.map(to_qlib)


def _numbers(raw: pd.DataFrame, column: str) -> pd.Series:
    return pd.to_numeric(raw[column], errors="coerce")


def _finite(
    values: pd.Series,
    label: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> pd.Series:
    array = values.to_numpy(dtype="float64", na_value=np.nan)
    if not np.isfinite(array).all():
        raise TushareProviderError(f"{label} must contain finite numeric values")
    if positive and (array <= 0).any():
        raise TushareProviderError(f"{label} must be positive")
    if nonnegative and (array < 0).any():
        raise TushareProviderError(f"{label} must be non-negative")
    return values


def _reject_duplicates(frame: pd.DataFrame) -> None:
    if frame.duplicated(subset=["instrument", "date"]).any():
        raise TushareProviderError("Tushare response contains duplicate instrument/date rows")


def normalize_daily(raw: pd.DataFrame) -> pd.DataFrame:
    """Normalize ``daily``: shares, yuan, and fractional change."""
    empty = _empty_frame(raw, _DAILY_COLUMNS)
    if empty is not None:
        return empty
    _require_columns(raw, _DAILY_REQUIRED)

    prices = {
        column: _finite(_numbers(raw, column), column, positive=True)
        for column in ("open", "high", "low", "close", "pre_close")
    }
    volume = _finite(_numbers(raw, "vol"), "vol", nonnegative=True) * 100.0
    amount = _finite(_numbers(raw, "amount"), "amount", nonnegative=True) * 1000.0
    change = _finite(_numbers(raw, "pct_chg"), "pct_chg") / 100.0

    result = pd.DataFrame(
        {
            "instrument": _instruments(raw),
            "date": _parse_dates(raw["trade_date"]),
            **prices,
            "volume": volume,
            "amount": amount,
            "change": change,
        }
    )
    _reject_duplicates(result)
    return result.reset_index(drop=True)


def normalize_adj_factor(raw: pd.DataFrame) -> pd.DataFrame:
    """Normalize ``adj_factor``; the raw cumulative factor is preserved."""
    empty = _empty_frame(raw, _ADJ_COLUMNS)
    if empty is not None:
        return empty
    _require_columns(raw, ("ts_code", "trade_date", "adj_factor"))

    result = pd.DataFrame(
        {
            "instrument": _instruments(raw),
            "date": _parse_dates(raw["trade_date"]),
            "adj_factor": _finite(_numbers(raw, "adj_factor"), "adj_factor", positive=True),
        }
    )
    _reject_duplicates(result)
    return result.reset_index(drop=True)


def normalize_calendar(raw: pd.DataFrame) -> pd.DataFrame:
    """Normalize ``trade_cal`` to a sorted ``date``/``is_open`` table."""
    empty = _empty_frame(raw, _CALENDAR_COLUMNS)
    if empty is not None:
        return empty
    _require_columns(raw, ("cal_date", "is_open"))

    flags = pd.to_numeric(raw["is_open"], errors="coerce")
    if flags.isna().any() or not bool(flags.isin((0, 1)).all()):
        raise TushareProviderError("is_open must contain only 0 or 1")

    result = pd.DataFrame({"date": _parse_dates(raw["cal_date"]), "is_open": flags.astype(bool)})
    return result.sort_values("date").reset_index(drop=True)


def normalize_index_daily(raw: pd.DataFrame) -> pd.DataFrame:
    """Normalize ``index_daily`` with shares/yuan and ascending trade dates."""
    empty = _empty_frame(raw, _INDEX_COLUMNS)
    if empty is not None:
        return empty
    _require_columns(raw, _INDEX_REQUIRED)

    result = pd.DataFrame(
        {
            "instrument": _instruments(raw),
            "date": _parse_dates(raw["trade_date"]),
            **{
                column: _finite(_numbers(raw, column), column, positive=True)
                for column in ("open", "high", "low", "close", "pre_close")
            },
            "volume": _finite(_numbers(raw, "vol"), "vol", nonnegative=True) * 100.0,
            "amount": _finite(_numbers(raw, "amount"), "amount", nonnegative=True) * 1000.0,
        }
    )
    if "pct_chg" in raw.columns:
        result["change"] = _finite(_numbers(raw, "pct_chg"), "pct_chg") / 100.0
    _reject_duplicates(result)
    return result.sort_values("date").reset_index(drop=True)


def normalize_daily_basic(raw: pd.DataFrame) -> pd.DataFrame:
    """Normalize ``daily_basic`` valuation fields, preserving missing as NaN."""
    empty = _empty_frame(raw, _BASIC_COLUMNS)
    if empty is not None:
        return empty
    _require_columns(raw, ("ts_code", "trade_date"))

    result = pd.DataFrame(
        {
            "instrument": _instruments(raw),
            "date": _parse_dates(raw["trade_date"]),
        }
    )
    for column in ("pe_ttm", "pb", "total_mv"):
        if column in raw.columns:
            result[column] = pd.to_numeric(raw[column], errors="coerce").replace([np.inf, -np.inf], np.nan)
        else:
            result[column] = np.nan
    result["total_mv"] = result["total_mv"] * 10000.0
    _reject_duplicates(result)
    return result.reset_index(drop=True)


def financial_request_fields(statement: str) -> str:
    """The ``fields`` argument for one statement API: keys plus the stored values."""
    _, values = FINANCIAL_STATEMENTS[statement]
    return ",".join((*_FINANCIAL_RAW_KEYS, *values))


def _optional_dates(raw: pd.DataFrame, column: str) -> pd.Series:
    if column not in raw.columns:
        return pd.Series(pd.NaT, index=raw.index, dtype="datetime64[ns]")
    text = raw[column].astype("string").str.strip()
    return pd.to_datetime(text, format="%Y%m%d", errors="coerce")


def _optional_text(raw: pd.DataFrame, column: str) -> pd.Series:
    if column not in raw.columns:
        return pd.Series(pd.NA, index=raw.index, dtype="string")
    return raw[column].astype("string").str.strip().replace("", pd.NA)


def normalize_financial_statement(raw: pd.DataFrame, statement: str, report_type: int, ts_code: str) -> pd.DataFrame:
    """Normalize one ``income``/``balancesheet``/``cashflow`` response for one stock.

    Dates the vendor leaves empty or malformed stay ``NaT``: point-in-time rules
    quarantine such rows instead of guessing an announcement date here.
    """
    if statement not in FINANCIAL_STATEMENTS:
        raise TushareProviderError("unknown financial statement")
    columns = [*FINANCIAL_KEY_COLUMNS, *FINANCIAL_VALUE_FIELDS]
    empty = _empty_frame(raw, columns)
    if empty is not None:
        return empty
    _require_columns(raw, ("ts_code", "end_date"))
    codes = raw["ts_code"].astype("string").str.strip().str.upper()
    if not bool(codes.eq(ts_code).all()):
        raise TushareProviderError("Tushare financial response contains another stock")
    if "report_type" in raw.columns:
        types = pd.to_numeric(raw["report_type"], errors="coerce")
        if not bool(types.eq(report_type).all()):
            raise TushareProviderError("Tushare financial response contains another report type")
    result = pd.DataFrame(
        {
            "ts_code": codes.astype(str),
            "statement": statement,
            "report_type": int(report_type),
            "comp_type": _optional_text(raw, "comp_type"),
            "report_period": _optional_dates(raw, "end_date"),
            "ann_date": _optional_dates(raw, "ann_date"),
            "f_ann_date": _optional_dates(raw, "f_ann_date"),
            "update_flag": _optional_text(raw, "update_flag"),
        }
    )
    for name in FINANCIAL_VALUE_FIELDS:
        values = pd.to_numeric(raw[name], errors="coerce") if name in raw.columns else np.nan
        result[name] = pd.Series(values, index=raw.index, dtype="float64").replace([np.inf, -np.inf], np.nan)
    return result.reset_index(drop=True)


__all__ = [
    "FINANCIAL_STATEMENTS",
    "FINANCIAL_VALUE_FIELDS",
    "TushareConfigError",
    "TushareProvider",
    "TushareProviderError",
    "TushareSettings",
    "financial_request_fields",
    "normalize_adj_factor",
    "normalize_calendar",
    "normalize_daily",
    "normalize_daily_basic",
    "normalize_financial_statement",
    "normalize_index_daily",
]
