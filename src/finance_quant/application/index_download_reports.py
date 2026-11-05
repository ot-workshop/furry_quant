"""Human-readable warehouse handling reports, derived without fetching data.

Reporting is independent of the market-validation fingerprint: adding a view
must not invalidate or rewrite an already verified immutable warehouse.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pandas as pd
from filelock import FileLock

from finance_quant.checksums import file_sha256, sha256_parts, sha256_text
from finance_quant.data.catalog import utc_now
from finance_quant.data.managed import atomic_json

LEVELS = {
    "single_source_passed": "单源检查通过",
    "dual_source_verified": "双源核验通过",
    "secondary_fallback_passed": "BaoStock补缺通过",
}
RULES = [
    "报价缺口：先查官方逐日停复牌记录，只有明确全天停牌才沿用此前价格估值，成交量为零。",
    "盘中停牌：保留当天真实成交，不按全天停牌处理。",
    "官方停复牌状态有歧义：保留冲突记录，改用独立且一致的交易状态；无法确认则待复核。",
    "辅助复权序列内部异常：最多切换另一复权口径，对完整区间重新核验，不拼接绝对因子。",
    "抽样辅助源不可用：官方自检通过时保留单源状态；依赖辅助源才能解决的问题继续阻断。",
    "主源行情或复权确有问题：辅助数据严格检查通过后，才允许整个股票区间回退；估值和涨跌停仍用官方。",
    "未知大幅涨跌或明确两源冲突：隔离并说明原因；新的公司行动需要补充可靠证据后才能放行。",
    "限频或访问限制：保留成功缓存并暂停需要该来源的操作，解除后续传。",
]


def _action(rule, title, reason, handling, *, dates=(), evidence=(), kind="handled"):
    return {
        "rule": rule,
        "title": title,
        "reason": reason,
        "handling": handling,
        "dates": sorted(set(dates)),
        "evidence": list(evidence),
        "kind": kind,
    }


def _request_evidence(root, request):
    """Expose only whitelisted public request parameters and content hashes."""
    params = request.get("params", {})
    result = {
        "source": "官方Tushare",
        "api": request.get("api", ""),
        "params": {k: params[k] for k in ["ts_code", "start_date", "end_date", "trade_date"] if k in params},
        "sha256": request.get("sha256", ""),
    }
    specification = {k: request[k] for k in ["api", "params", "provider_id"] if k in request}
    if len(specification) == 3 and request.get("api") == "suspend_d":
        key = sha256_text(json.dumps(specification, sort_keys=True))[:16]
        result["file"] = f"suspension-raw/suspend_d-{key}.parquet"
    return result


def _secondary_evidence(metadata):
    return {
        "source": "BaoStock",
        "adjusted_flag": metadata.get("adjusted_flag", "1"),
        "cache_key": metadata.get("cache_key", ""),
        "sha256": metadata.get("files", {}),
    }


def _official_rows(root, request):
    evidence = _request_evidence(root, request)
    relative = evidence.get("file")
    if not relative:
        return None, evidence
    path = root / relative
    if not path.is_file() or file_sha256(path) != evidence["sha256"]:
        return None, evidence
    return pd.read_parquet(path), evidence


def describe_receipt(root, receipt, name=""):
    """Explain actual persisted decisions; never infer a repair from the ticker."""
    code = receipt["code"]
    level = receipt.get("validation_level", "")
    report = receipt.get("cross_validation", {})
    quality = receipt.get("quality", {})
    actions = []
    halt = report.get("suspension_evidence") or {}
    if not halt and report.get("stocks"):
        checked = report["stocks"][0]
        if checked.get("suspended_dates") and checked.get("secondary"):
            halt = {"source": checked["secondary"], "dates": checked["suspended_dates"]}
    if halt.get("dates"):
        official = halt.get("source") == "tushare_suspend_d"
        evidence = (
            [_request_evidence(root, r) for r in halt.get("requests", [])]
            if official
            else [_secondary_evidence(halt.get("source", {}))]
        )
        actions.append(
            _action(
                "official_full_day_halt" if official else "independent_halt_status",
                "官方确认全天停牌" if official else "独立交易状态确认停牌",
                f"报价缺失或无成交，经交易状态确认{len(halt['dates'])}个全天停牌日。",
                "仅沿用此前收盘价和此前复权因子估值，成交量为零；可用估值字段仅沿用此前值。该证据不升级为双源价格认证。",
                dates=halt["dates"],
                evidence=evidence,
            )
        )
        # Explain intraday records only where the saved market really retained
        # observed activity. No provider annotation alone certifies a trade.
        market_path = (root / receipt["file"]).resolve()
        market = None
        for request in halt.get("requests", []):
            raw, source = _official_rows(root, request)
            if raw is None or not {"ts_code", "trade_date", "suspend_type", "suspend_timing"}.issubset(raw):
                continue
            intraday = raw.loc[
                raw.ts_code.eq(code)
                & raw.suspend_type.eq("S")
                & raw.suspend_timing.fillna("").astype(str).str.strip().ne("")
            ]
            if intraday.empty:
                continue
            if market is None:
                if not market_path.is_relative_to(root) or not market_path.is_file():
                    continue
                if file_sha256(market_path) != receipt["sha256"]:
                    raise ValueError("报告对应的行情文件校验失败")
                market = pd.read_parquet(market_path, columns=["date", "is_suspended", "volume", "amount"])
            days = pd.to_datetime(intraday.trade_date, format="%Y%m%d")
            retained = market.loc[
                market.date.isin(days) & ~market.is_suspended & market.volume.gt(0) & market.amount.gt(0), "date"
            ]
            if not retained.empty:
                actions.append(
                    _action(
                        "intraday_halt",
                        "盘中停牌保留真实成交",
                        "官方记录是日内部分时段停牌，已保存行情仍有真实成交。",
                        "保留当日原始价格、成交量和成交额，不生成全天停牌估值行。",
                        dates=retained.dt.strftime("%Y-%m-%d").tolist(),
                        evidence=[source],
                    )
                )
    for review in report.get("official_status_reviews", []):
        raw, evidence = _official_rows(root, review.get("request", {}))
        dates = []
        if raw is not None and {"trade_date", "suspend_type"}.issubset(raw):
            dates = [
                pd.Timestamp(str(day)).strftime("%Y-%m-%d")
                for day, rows in raw.groupby("trade_date")
                if {"S", "R"}.issubset(set(rows.suspend_type))
            ]
        actions.append(
            _action(
                "official_status_conflict",
                "官方停复牌状态存在歧义",
                "同一查询月份存在不能单独证明全天停牌的停复牌记录。",
                "保留原始冲突，使用通过检查的独立交易状态确认；最终来源等级仍按价格和复权证据决定。",
                dates=dates,
                evidence=[evidence],
            )
        )
    for rejected in report.get("rejected_secondary_bases", []):
        stock = (report.get("stocks") or [{}])[0]
        selected = (stock.get("secondary") or {}).get("adjusted_flag", "1")
        actions.append(
            _action(
                "adjustment_basis_retry",
                "切换辅助复权口径重新核验",
                "原辅助复权序列未通过内部一致性检查。",
                f"保留失败捕获，改用复权口径{selected}对整个区间重新核验，不修改单日绝对因子。",
                dates=rejected.get("dates", []),
                evidence=[_secondary_evidence(rejected.get("source_evidence", {}))],
            )
        )
    if report.get("status") == "unavailable":
        actions.append(
            _action(
                "secondary_unavailable",
                "抽样辅助核验不可用",
                "辅助来源连接、配额或内部数据检查未通过；官方自检已通过。",
                "保留官方数据，明确标为单源检查通过，不声称双源核验通过。",
                dates=report.get("validation_dates", []),
                kind="warning",
            )
        )
    if level == "secondary_fallback_passed":
        actions.append(
            _action(
                "whole_interval_fallback",
                "整个股票区间回退",
                "主源价格或复权未通过检查，辅助区间已严格核验。",
                "整个区间的行情与相对复权因子使用BaoStock，估值及涨跌停仍使用官方Tushare。",
                dates=[
                    pd.Timestamp(receipt["start"]).strftime("%Y-%m-%d"),
                    pd.Timestamp(receipt["end"]).strftime("%Y-%m-%d"),
                ],
            )
        )
    for event in quality.get("verified_event_moves", []):
        actions.append(
            _action(
                "reviewed_event",
                "已核实的特殊交易日",
                "大幅收益触发审核，但存在早于交易日的已审核公开事件证据且收益一致性检查通过。",
                "保留真实涨跌，标记该日不设日涨跌幅限制；新的未知事件不会套用此审核。",
                dates=[event["date"]],
                evidence=[{"source": "已审核公开公告", "url": event["source"], "known_by": event["known_by"]}],
            )
        )
    missing_pe = quality.get("missing_positive_pe", 0)
    if missing_pe:
        actions.append(
            _action(
                "unavailable_positive_pe",
                "估值因子可用性提示",
                f"{missing_pe}个交易日的PE缺失、非正或不可用。",
                "保留真实值或缺失，不编造正PE；使用该字段的因子按有效性规则处理。",
                kind="info",
            )
        )
    handled = any(a["kind"] in {"handled", "warning"} for a in actions)
    return {
        "code": code,
        "name": name,
        "validation_level": level,
        "result": LEVELS.get(level, "通过等级未记录"),
        "outcome": "auto_handled" if handled else "passed",
        "actions": actions,
        "receipt_file": str((Path(receipt["file"]).parent / "receipt.json").as_posix()),
        "market_sha256": receipt.get("sha256", ""),
    }


def _failure(code, failure, name):
    reason = failure.get("reason", "")
    labels = {
        "BaoStock adjusted returns disagree with its reported changes": "辅助复权序列与自身涨跌幅不一致",
        "Conflicting official full-day suspension records": "官方全天停牌记录存在歧义",
        "Suspension evidence has wrong instrument or adjustment basis": "辅助交易状态的股票或复权口径不符",
        "Secondary trading activity conflicts with suspension status": "辅助停牌状态与成交活动冲突",
        "两源行情、复权或停牌校验存在冲突": "两个有效来源的行情、复权或停牌证据存在冲突",
    }
    return {
        "code": code,
        "name": name,
        "validation_level": None,
        "result": "待复核，未发布",
        "outcome": "needs_review",
        "actions": [
            _action(
                "unresolved",
                "自动处理未能确认",
                labels.get(reason, "来源、覆盖或一致性检查未通过"),
                "保留原始捕获和已通过缓存，当前股票不进入已通过数据；需补充可靠证据、修订数据或解除来源限制后续传。",
                dates=failure.get("dates", []),
                kind="unresolved",
            )
        ],
    }


def refresh_handling_report(root):
    """Persist one current report and a per-job copy; no network or data edits."""
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with FileLock(root / "handling-report.lock", timeout=15):
        path = root / "progress.json"
        plan_path = root / "plan.json"
        progress_bytes = path.read_bytes() if path.exists() else b"{}"
        plan_bytes = plan_path.read_bytes() if plan_path.exists() else b"{}"
        source_id = sha256_parts((progress_bytes, plan_bytes, Path(__file__).read_bytes()))
        saved = root / "handling-report.json"
        if saved.exists():
            cached = json.loads(saved.read_text(encoding="utf-8"))
            if cached.get("source_id") == source_id:
                return cached
        progress = json.loads(progress_bytes)
        plan = json.loads(plan_bytes)
        tasks = {t["code"]: t for t in plan.get("tasks", [])}
        stocks = []
        failures = progress.get("failures", {})
        for code, asset in sorted(progress.get("assets", {}).items()):
            if code in failures:
                continue
            receipt_path = (root / asset["file"]).with_name("receipt.json").resolve()
            if not receipt_path.is_relative_to(root):
                raise ValueError("报告资产路径无效")
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if receipt["code"] != code or receipt["sha256"] != asset["sha256"]:
                raise ValueError("报告资产来源不一致")
            stocks.append(describe_receipt(root, receipt, tasks.get(code, {}).get("name", "")))
        stocks += [
            _failure(code, failure, tasks.get(code, {}).get("name", "")) for code, failure in sorted(failures.items())
        ]
        actions = Counter(a["rule"] for s in stocks for a in s["actions"])
        passed = sum(s["outcome"] != "needs_review" for s in stocks)
        report = {
            "schema_version": "index-handling-report-v1",
            "source_id": source_id,
            "job_id": progress.get("job_id"),
            "status": progress.get("status", "not_started"),
            "source_updated_at": progress.get("updated_at"),
            "generated_at": utc_now(),
            "scope": "当前冻结历史仓库；不是自动扩展到最新日期，也不改写历史行情",
            "summary": {
                "total": progress.get("total", len(tasks)),
                "passed": passed,
                "needs_review": len(failures),
                "pending": max(0, progress.get("total", len(tasks)) - len(stocks)),
                "auto_handled": sum(s["outcome"] == "auto_handled" for s in stocks),
                "validation_counts": dict(Counter(s["validation_level"] for s in stocks if s["validation_level"])),
                "action_counts": dict(actions),
            },
            "rules": RULES,
            "stocks": stocks,
        }
        if progress.get("status") == "paused":
            report["job_handling"] = (
                "任务已暂停，成功缓存已保留；限频或访问限制解除后可续传。抽样不可用不会伪装成双源通过。"
            )
        atomic_json(saved, report)
        job = progress.get("job_id")
        if isinstance(job, str) and job and all(c.isalnum() or c in "-_" for c in job):
            atomic_json(root / "handling-reports" / f"{job}.json", report)
        return report
