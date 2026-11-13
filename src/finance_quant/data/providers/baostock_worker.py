"""Isolated worker process for the BaoStock SDK.

The SDK mutates global socket/session state and may block forever on
``recv(b"")``.  This module is invoked by :mod:`...providers.baostock` in a
separate process, emits one JSON document on stdout, and never prints SDK
messages or raw exception text.
"""

from __future__ import annotations

import contextlib
import io
import json
import socket
import sys
from datetime import datetime
from typing import Any

import baostock
import baostock.util.socketutil as socketutil

from finance_quant.data import network_budget
from finance_quant.data.network_budget import DownloadPaused, NetworkBudget

FIELDS = (
    "date",
    "code",
    "open",
    "high",
    "low",
    "close",
    "preclose",
    "volume",
    "amount",
    "adjustflag",
    "pctChg",
    "tradestatus",
    "isST",
)
_FIELDS_ARG = ",".join(FIELDS)
_ERROR = "BaoStock worker failed"


class WorkerError(RuntimeError):
    """Internal worker failure; details are never exposed to the caller."""


def _normalize_date(value: Any) -> str:
    if not isinstance(value, str):
        raise WorkerError("invalid request")
    try:
        if len(value) == 8 and value.isdigit():
            parsed = datetime.strptime(value, "%Y%m%d").date()
        elif len(value) == 10 and value[4] == "-" and value[7] == "-":
            parsed = datetime.strptime(value, "%Y-%m-%d").date()
        else:
            raise ValueError
    except ValueError:
        raise WorkerError("invalid request") from None
    return parsed.isoformat()


def _baostock_code(code: str) -> str:
    if not isinstance(code, str) or len(code) != 9 or "." not in code:
        raise WorkerError("invalid request")
    number, suffix = code.split(".", 1)
    if len(number) != 6 or not number.isdigit() or suffix not in ("SH", "SZ"):
        raise WorkerError("invalid request")
    return f"{suffix.lower()}.{number}"


def _request_parts(request: Any) -> tuple[str, str, str]:
    if (
        not isinstance(request, dict)
        or not {"code", "start", "end"}.issubset(request)
        or set(request) - {"code", "start", "end", "adjusted_flag"}
    ):
        raise WorkerError("invalid request")
    if request.get("adjusted_flag", "1") not in {"1", "2"}:
        raise WorkerError("invalid adjustment basis")
    code = _baostock_code(request["code"])
    start = _normalize_date(request["start"])
    end = _normalize_date(request["end"])
    if start > end:
        raise WorkerError("invalid request")
    return code, start, end


def _sdk():

    NetworkBudget().assert_available("baostock", "sdk_message", daily=10000)
    # Every SDK message passes here, including login/logout and pagination.
    original = socketutil.send_msg

    def counted(message):
        network_budget.reserve_baostock()
        return original(message)

    socketutil.send_msg = counted

    return baostock


def _ok(result: Any) -> bool:
    if str(getattr(result, "error_code", "")) == "10001011":
        NetworkBudget().hold("baostock")
        raise DownloadPaused("BaoStock限制访问，下载已暂停")
    return result is not None and str(getattr(result, "error_code", "")) == "0"


def _collect(result: Any) -> list[dict[str, str]]:
    if not _ok(result):
        raise WorkerError("query failed")
    if list(getattr(result, "fields", ())) != list(FIELDS):
        raise WorkerError("unexpected fields")

    rows: list[dict[str, str]] = []
    with contextlib.redirect_stdout(io.StringIO()):
        while result.next():
            if not _ok(result):
                raise WorkerError("query failed")
            row = result.get_row_data()
            if not isinstance(row, list) or len(row) != len(FIELDS):
                raise WorkerError("invalid row")
            rows.append(dict(zip(FIELDS, row)))
    if not _ok(result):
        raise WorkerError("query failed")
    if not rows:
        raise WorkerError("empty result")
    return rows


def _query(sdk, code: str, start: str, end: str, adjustflag: str):
    with contextlib.redirect_stdout(io.StringIO()):
        return sdk.query_history_k_data_plus(
            code,
            _FIELDS_ARG,
            start_date=start,
            end_date=end,
            frequency="d",
            adjustflag=adjustflag,
        )


def fetch_history(request: Any, sdk=None) -> dict[str, list[dict[str, str]]]:
    """Run one login, the raw and adjusted queries, and one logout."""
    code, start, end = _request_parts(request)
    if sdk is None:
        sdk = _sdk()

    logged_in = False
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            login_result = sdk.login()
        if not _ok(login_result):
            raise WorkerError("login failed")
        logged_in = True

        raw = _collect(_query(sdk, code, start, end, "3"))
        adjusted = _collect(_query(sdk, code, start, end, request.get("adjusted_flag", "1")))
    finally:
        if logged_in:
            with contextlib.redirect_stdout(io.StringIO()):
                try:
                    sdk.logout()
                except Exception:
                    pass

    return {"raw": raw, "adjusted": adjusted}


def main(argv=None) -> int:
    socket.setdefaulttimeout(15)
    try:
        raw_request = sys.stdin.read()
        request = json.loads(raw_request)
        payload = fetch_history(request)
        output = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    except Exception as error:
        if isinstance(error, DownloadPaused):
            print(json.dumps({"error": _ERROR, "category": "quota_or_hold"}))
            return 3
        print(json.dumps({"error": _ERROR}, separators=(",", ":")))
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
