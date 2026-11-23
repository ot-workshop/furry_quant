"""Internal entry point for registering one fixed-pool forward validation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time

from sqlalchemy import Engine

from finance_quant.data.symbols import split_ts_code
from finance_quant.persistence.forward import SHANGHAI, canonical_sha256
from finance_quant.persistence.forward_registration import RegistrationReceipt, register_and_confirm
from finance_quant.provenance import source_provenance
from finance_quant.research.registry import FactorSpec


@dataclass(frozen=True)
class ForwardRegistrationRequest:
    name: str
    signal_date: date
    pool_codes: tuple[str, ...]
    top_k: int
    factor_window: int
    factor_direction: str
    dataset_id: str | None = None


@dataclass(frozen=True)
class ForwardCalendar:
    version: str
    sessions: tuple[date, ...]


def register_forward(
    engine: Engine,
    request: ForwardRegistrationRequest,
    calendar: ForwardCalendar,
    *,
    key: str,
) -> RegistrationReceipt:
    """Freeze the submitted strategy against the supplied versioned exchange calendar."""
    if type(request.signal_date) is not date:
        raise ValueError("信号日必须是日期")
    document = {
        "name": request.name.strip(),
        "signal_date": request.signal_date.isoformat(),
        "pool_codes": sorted(code.strip().upper() for code in request.pool_codes),
        "top_k": request.top_k,
        "factor": "momentum",
        "factor_window": request.factor_window,
        "factor_direction": request.factor_direction,
        "dataset_id": request.dataset_id,
    }
    return register_and_confirm(
        engine, key=key, request=document, freeze=lambda: _freeze_registration(document, request.signal_date, calendar)
    )


def _freeze_registration(document: dict, signal_date: date, calendar: ForwardCalendar) -> dict:
    codes = document["pool_codes"]
    top_k = document["top_k"]
    if not document["name"]:
        raise ValueError("登记名称不能为空")
    for code in codes:
        split_ts_code(code)
    if len(set(codes)) != len(codes):
        raise ValueError("固定股票池不能包含重复代码")
    if type(top_k) is not int or not 0 < top_k < len(codes):
        raise ValueError("前 K 必须是大于零且小于固定池股票数的整数")
    if type(document["factor_window"]) is not int:
        raise ValueError("动量窗口必须是整数")
    factor = FactorSpec("momentum", 1.0, document["factor_window"])
    if document["factor_direction"] not in ("high", "low"):
        raise ValueError("动量方向必须是 high 或 low")
    dataset_id = document["dataset_id"]
    if dataset_id is not None and (not isinstance(dataset_id, str) or not dataset_id.strip() or len(dataset_id) > 100):
        raise ValueError("数据集标识无效")

    if not calendar.version.strip() or len(calendar.version) > 40:
        raise ValueError("交易日历版本无效")
    if any(type(session) is not date for session in calendar.sessions):
        raise ValueError("交易日历必须由日期组成")
    sessions = sorted(set(calendar.sessions))
    if signal_date not in sessions:
        raise ValueError("信号日必须是冻结日历中的交易日")
    entry_index = sessions.index(signal_date) + 1
    next_sessions = sessions[entry_index : entry_index + 2]
    if len(next_sessions) != 2:
        raise ValueError("冻结日历必须覆盖入场日和退出日")
    entry_date, exit_date = next_sessions
    frozen_calendar = {"version": calendar.version, "trading_dates": [session.isoformat() for session in sessions]}
    provenance = source_provenance()
    lock_digest = provenance["dependency_lock_sha256"]
    if lock_digest is None:
        raise ValueError("缺少依赖锁，无法冻结登记证据")

    return {
        **document,
        "signal_date": signal_date,
        "entry_date": entry_date,
        "exit_date": exit_date,
        "calendar_version": calendar.version,
        "calendar_fingerprint": canonical_sha256(frozen_calendar),
        "pool_size": len(codes),
        "code_sha256": provenance["code_sha256"],
        "data_rules": {
            "calendar": frozen_calendar,
            "dependency_lock_sha256": lock_digest,
            "timezone": "Asia/Shanghai",
            "score_data_end": signal_date.isoformat(),
            "score_capture_after": "signal_close",
            "score_requires_trading_state": True,
            "label_capture_after": datetime.combine(exit_date, time(15), tzinfo=SHANGHAI).isoformat(),
            "label_snapshot": "first_eligible",
        },
        "strategy_rules": {
            "factor_parameters": factor.parameters,
            "tie_break": "code_ascending",
            "holding_sessions": 1,
            "portfolio_weighting": "equal",
            "benchmark": "equal_weight_fixed_pool",
            "return_price": "adjusted_open",
            "main_metric": "candidate_return_minus_pool_return_pp",
            "fees_included": False,
        },
    }
