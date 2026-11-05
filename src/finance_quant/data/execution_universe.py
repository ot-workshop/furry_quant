"""Explicit boundary of the currently implemented 100-share execution model."""

from __future__ import annotations


def validate_execution_universe(codes) -> None:
    unsupported = [
        code
        for code in codes
        if not (
            len(code) == 9
            and (
                (code.endswith(".SH") and code.startswith("60"))
                or (code.endswith(".SZ") and code.startswith(("00", "30")))
            )
        )
    ]
    if unsupported:
        raise ValueError(
            "当前回测支持沪深主板及创业板A股；科创板、北交所、B股、ETF和指数尚未适配数据或交易规则："
            + "、".join(sorted(unsupported))
        )
