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

DEFAULT_HTTP_URL = "https://api.tushare.pro"
MAX_RETRIES = 2

_API_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CODE_RE = re.compile(r"^\d{6}\.(?:SH|SZ|BJ)$")
_RETRY_DELAY = 0.25

_DAILY_COLUMNS = [
    "instrument", "date", "open", "high", "low", "close", "pre_close",
    "volume", "amount", "change",
]
_DAILY_REQUIRED = (
    "ts_code", "trade_date", "open", "high", "low", "close", "pre_close",
    "vol", "amount", "pct_chg",
)
_ADJ_COLUMNS = ["instrument", "date", "adj_factor"]
_CALENDAR_COLUMNS = ["date", "is_open"]
_INDEX_COLUMNS = [
    "instrument", "date", "open", "high", "low", "close", "pre_close",
    "volume", "amount",
]
_INDEX_REQUIRED = (
    "ts_code", "trade_date", "open", "high", "low", "close", "pre_close",
    "vol", "amount",
)
_BASIC_COLUMNS = ["instrument", "date", "pe_ttm", "pb", "total_mv"]


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
        http_url = (
            DEFAULT_HTTP_URL if self.http_url is None else str(self.http_url).strip()
        )
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
            raise TushareConfigError(
                "TUSHARE_HTTP_URL must be HTTPS without userinfo, query, or fragment"
            )
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

                file_values = {
                    key: value
                    for key, value in dotenv_values(path).items()
                    if value is not None
                }
        except OSError:
            file_values = {}

        token = (
            os.environ.get("TUSHARE_TOKEN")
            or file_values.get("TUSHARE_TOKEN")
            or ""
        )
        http_url = (
            os.environ.get("TUSHARE_HTTP_URL")
            or file_values.get("TUSHARE_HTTP_URL")
            or DEFAULT_HTTP_URL
        )
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
            "timeout", "timed out", "connection", "network", "请求异常",
            "temporarily unavailable", "remote end closed", "max retries",
        )
    )


def _error_category(exc: BaseException) -> str:
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
            try:
                import tushare as ts

                client = ts.pro_api(self.settings.token, timeout=20)
                client._DataApi__http_url = self.settings.http_url
            except Exception:
                raise TushareProviderError(
                    "failed to initialize the Tushare SDK client"
                ) from None
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


def _empty_frame(raw, columns: list[str]) -> pd.DataFrame | None:
    if not isinstance(raw, pd.DataFrame):
        raise TushareProviderError("Tushare response must be a pandas DataFrame")
    if raw.empty:
        return pd.DataFrame(columns=columns)
    return None


def _require_columns(raw: pd.DataFrame, required: tuple[str, ...]) -> None:
    missing = [column for column in required if column not in raw.columns]
    if missing:
        raise TushareProviderError(
            "Tushare response is missing required column(s): " + ", ".join(missing)
        )


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
    valid = codes.map(
        lambda code: isinstance(code, str) and _CODE_RE.fullmatch(code) is not None
    )
    if not bool(valid.all()):
        raise TushareProviderError("Tushare response contains an invalid ts_code")
    return codes.map(lambda code: f"{code[-2:]}{code[:6]}")


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
        raise TushareProviderError(
            "Tushare response contains duplicate instrument/date rows"
        )


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
            "adj_factor": _finite(
                _numbers(raw, "adj_factor"), "adj_factor", positive=True
            ),
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

    result = pd.DataFrame(
        {"date": _parse_dates(raw["cal_date"]), "is_open": flags.astype(bool)}
    )
    return result.sort_values("date").reset_index(drop=True)


def normalize_index_daily(raw: pd.DataFrame) -> pd.DataFrame:
    """Normalize ``index_daily`` with shares and yuan, like standard daily data."""
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
            "amount": _finite(
                _numbers(raw, "amount"), "amount", nonnegative=True
            ) * 1000.0,
        }
    )
    if "pct_chg" in raw.columns:
        result["change"] = (
            _finite(_numbers(raw, "pct_chg"), "pct_chg") / 100.0
        )
    _reject_duplicates(result)
    return result.reset_index(drop=True)


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


__all__ = [
    "TushareConfigError",
    "TushareProvider",
    "TushareProviderError",
    "TushareSettings",
    "normalize_adj_factor",
    "normalize_calendar",
    "normalize_daily",
    "normalize_daily_basic",
    "normalize_index_daily",
]
