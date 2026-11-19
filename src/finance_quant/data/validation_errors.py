"""Public, structured download failures shared by the worker and data manager."""

from __future__ import annotations


class MarketDataError(ValueError):
    def __init__(self, message, *, category, dates=(), action="", evidence=None):
        super().__init__(message)
        self.category = category
        self.validation_dates = sorted(set(dates))
        self.action = action
        self.evidence = evidence or {}


def failure_record(code, error):
    dates = getattr(error, "validation_dates", [])
    return {
        "code": code,
        "type": type(error).__name__,
        "reason": str(error)[:500]
        if isinstance(error, ValueError)
        else "数据源或运行检查失败；查看后台日志，已有数据保留。",
        "category": getattr(error, "category", "source_or_coverage_validation_failed"),
        "dates": dates[:50],
        "date_count": len(dates),
        "action": getattr(error, "action", "查看数据覆盖与来源明细，处理后续传；已有成功分区和原始响应保留。"),
        "evidence": getattr(error, "evidence", {}),
    }
