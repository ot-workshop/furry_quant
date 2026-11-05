"""Canonical conversions between provider and Qlib security identifiers."""

from __future__ import annotations

import re

TS_CODE = re.compile(r"^(?P<number>\d{6})\.(?P<exchange>SH|SZ|BJ)$")


def split_ts_code(code: str) -> tuple[str, str]:
    """Return ``(number, exchange)`` for one canonical A-share code."""
    match = TS_CODE.fullmatch(code)
    if match is None:
        raise ValueError(f"Invalid A-share code: {code}")
    return match.group("number"), match.group("exchange")


def to_qlib(code: str) -> str:
    """Convert ``600000.SH`` to Qlib's ``SH600000`` form."""
    number, exchange = split_ts_code(code)
    return exchange + number


def to_baostock(code: str) -> str:
    """Convert ``600000.SH`` to BaoStock's ``sh.600000`` form."""
    number, exchange = split_ts_code(code)
    if exchange == "BJ":
        raise ValueError("BaoStock does not support Beijing Stock Exchange codes")
    return f"{exchange.lower()}.{number}"


def exchange_of(code: str) -> str:
    """Return the canonical exchange suffix."""
    return split_ts_code(code)[1]
