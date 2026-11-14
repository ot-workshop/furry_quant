"""Child process that executes exactly one claimed task; Qlib never runs in the API process."""

from __future__ import annotations

import argparse
import errno
import logging
import re
import traceback
from pathlib import Path

from filelock import FileLock

from finance_quant.application import (
    data_updates,
    financial_download,
    historical_workflow,
    industry_download,
    market_download,
)
from finance_quant.application.backup_jobs import execute
from finance_quant.application.evaluation_workflow import evaluate_configured
from finance_quant.application.experiment_tasks import run_experiment_task
from finance_quant.application.factor_workflow import publish_factor_run
from finance_quant.application.real_workflow import run_configured
from finance_quant.application.research_requests import request_config, validate_research_data
from finance_quant.application.task_context import (
    TaskContext,
    attach_outputs,
    financial_binding,
    prepare_snapshot,
    prepare_warehouse,
)
from finance_quant.application.task_context import register_snapshot_dataset as register_snapshot_dataset
from finance_quant.data.managed import DatasetDownloadError
from finance_quant.data.network_budget import DownloadPaused
from finance_quant.data.postgres_market import PostgresMarketStore
from finance_quant.data.shared_market import configure_store
from finance_quant.persistence.market import MarketRepository
from finance_quant.persistence.results import PgResultStore
from finance_quant.persistence.tasks import PgTaskStore

LOGGER = logging.getLogger(__name__)


def safe_error(error):

    if isinstance(error, DatasetDownloadError):
        return "数据下载或联合校验未通过，请检查服务权限、股票代码与日期；重新运行会复用已完成缓存。"
    if isinstance(error, OSError) and (error.errno == errno.ENOSPC or getattr(error, "winerror", None) in (39, 112)):
        return "存储空间不足，无法写入研究结果；请释放相关磁盘空间后重试，任务参数已保留。"
    if isinstance(error, (ValueError, DownloadPaused)):
        text = str(error)[:500]
        text = re.sub(r"(?:tsr_|sk-)[A-Za-z0-9_-]+", "[已隐藏]", text)
        text = re.sub(r"(?i)(token|password|authorization|secret)\s*[:=]\s*\S+", r"\1=[已隐藏]", text)
        return text
    if isinstance(error, MemoryError):
        return "研究计算内存不足，已保留任务参数，请重试。"
    return f"研究计算出现内部错误（{type(error).__name__}），已保存失败阶段与诊断信息。"


def runtime_failure_details(error, stage, identifier):
    labels = {
        "prepare": "准备数据",
        "factors": "计算因子",
        "backtest": "回测",
        "evaluation": "因子评估",
        "experiment": "方案比较",
        "download": "下载数据",
    }
    summary = (
        f"{labels.get(stage, stage)}内存不足，已保留任务参数，请重试。"
        if isinstance(error, MemoryError)
        else safe_error(error)
    )
    # Save locations without exception arguments, local variables or credentials.
    stack = [
        {"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
        for frame in traceback.extract_tb(error.__traceback__)
    ]
    traceback.clear_frames(error.__traceback__)
    return {
        "kind": "runtime",
        "job_id": identifier,
        "issues": [],
        "stage": stage,
        "exception_type": type(error).__name__,
        "summary": summary,
        "trace": stack,
    }


def run_task(
    runs_root: Path,
    data_root: Path,
    identifier: str,
    *,
    task_store,
    result_store_factory,
    market_repository=None,
    storage_root: Path | None = None,
):
    """Execute one claimed task. Failures are recorded on the task, never raised."""
    store = task_store
    storage_root = (storage_root or runs_root.parent).resolve()
    with FileLock(runs_root / "tasks/execution.lock", timeout=0):
        task = store.get(identifier, private=True)
        if task["status"] != "running":
            raise ValueError("Only a claimed task can execute")
        kind = task.get("kind", "research")
        if kind == "maintenance":
            try:
                return execute(store, task, root=storage_root)
            except Exception as error:
                details = runtime_failure_details(error, "download", identifier)
                store.finish(identifier, "failed", error=details["summary"], error_details=details)
                return
        if kind == "data_update":
            return run_data_update_task(store, task, data_root)
        if kind == "market_download":
            return run_download_task(store, task, data_root)
        if kind == "financial_download":
            return run_financial_download_task(store, task, data_root)
        if kind == "industry_download":
            return run_industry_download_task(store, task)
        context = TaskContext(
            identifier, store, runs_root, data_root, storage_root, result_store_factory, market_repository
        )
        try:
            if kind == "experiment":
                result = run_experiment_task(context, task["request"], task["execution"])
            elif task["execution"]["mode"] in {"index_warehouse", "market_warehouse"}:
                result = _run_historical(context, task["request"], task["execution"])
            else:
                result = _run_snapshot(context, task["request"], task["execution"])
            store.finish(identifier, "succeeded", result=result)
        except Exception as error:
            details = runtime_failure_details(error, store.get(identifier)["stage"], identifier)
            LOGGER.error("Task %s failed at %s: %s", identifier, details["stage"], details["summary"])
            store.finish(identifier, "failed", error=details["summary"], error_details=details)


def _run_historical(context: TaskContext, request: dict, execution: dict) -> dict:

    root, dataset_record = prepare_warehouse(context, request, execution)
    binding = financial_binding(execution, context.data_root)
    result_store = context.result_store_factory(dataset_record["id"] if dataset_record else None)
    result = historical_workflow.run_historical_research(
        request,
        root,
        context.runs_root,
        expected_sha=execution["manifest_sha256"],
        stage=context.stage,
        result_store=result_store,
        financials=binding,
    )
    if dataset_record:
        attach_outputs(result_store, dataset_record["id"], result)
        result["dataset_id"] = dataset_record["id"]
    return result


def _run_snapshot(context: TaskContext, request: dict, execution: dict) -> dict:
    context.stage("prepare", "准备所选股票池并检查预热数据", 0)
    market, calendar, manifest, data, dataset_record = prepare_snapshot(context, request, execution)
    if "filters" in request["data"]:
        result_store = context.result_store_factory(dataset_record["id"] if dataset_record else None)
        result = historical_workflow.run_historical_research(
            request,
            data.root,
            context.runs_root,
            stage=context.stage,
            result_store=result_store,
            financials=financial_binding(execution, context.data_root),
        )
        if dataset_record:
            attach_outputs(result_store, dataset_record["id"], result)
            result["dataset_id"] = dataset_record["id"]
        return result
    config = request_config(request, data, context.runs_root, context.data_root)
    validate_research_data(config, market, calendar)
    result_store = context.result_store_factory(dataset_record["id"] if dataset_record else None)
    context.stage("factors", "计算因子原始值、评分与排名", 1)
    factors = publish_factor_run(
        market,
        manifest,
        context.runs_root,
        config.factors,
        config.neutralization,
        result_store=result_store,
        financials=config.financials,
    )
    context.stage("backtest", "Qlib 正在模拟交易、费用和账户净值", 2)
    backtest = run_configured(config, result_store=result_store)
    context.stage("evaluation", "计算因子 IC、分组收益与诊断报告", 3)
    evaluation = evaluate_configured(config, result_store=result_store)
    if not (factors["metadata"]["id"] == backtest["factor_run_id"] == evaluation["factor_run_id"]):
        raise ValueError("Factor references differ across research outputs")
    result = {
        "data_version": manifest["id"],
        "factor_run_id": factors["metadata"]["id"],
        "backtest_run_id": backtest["run_id"],
        "evaluation_run_id": evaluation["run_id"],
        "backtest_reused": backtest["reused"],
        "evaluation_reused": evaluation["reused"],
    }
    if dataset_record:
        attach_outputs(result_store, dataset_record["id"], result)
        result["dataset_id"] = dataset_record["id"]
    return result


def run_download_task(store, task, data_root):
    identifier = task["id"]
    root = Path(task["execution"]["root"]).resolve()
    if not root.is_relative_to(data_root.resolve()):
        store.finish(identifier, "failed", error="下载目录超出数据根目录")
        return
    try:
        store.stage(identifier, "download", "下载并检查数据", 0)
        (root / "PAUSE").unlink(missing_ok=True)

        result = market_download.run(root, task["execution"]["plan_id"])
        succeeded = result["status"] == "complete"
        store.finish(
            identifier,
            "succeeded" if succeeded else "interrupted",
            result={
                key: result[key]
                for key in ("id", "status", "version_id", "completed", "total", "message")
                if key in result
            },
            error=None if succeeded else result.get("message", "数据尚未完整，请查看下载明细后续传"),
        )
    except Exception as error:
        details = runtime_failure_details(error, "download", identifier)
        store.finish(identifier, "failed", error=details["summary"], error_details=details)


def run_data_update_task(store, task, data_root):

    identifier = task["id"]
    try:
        execution = task["execution"]
        plan_id = execution.get("plan_id")
        if not plan_id:
            plan_id = data_updates.preview(data_root, execution["spec"])["id"]
        result = data_updates.run(
            data_root,
            plan_id,
            on_progress=lambda row: store.stage(
                identifier, "download", row.get("message", row.get("current_stage", "更新数据")), row["completed"]
            ),
        )
        succeeded = result["status"] == "complete"
        store.finish(
            identifier,
            "succeeded" if succeeded else "interrupted",
            result=result,
            error=None if succeeded else result.get("message", "数据更新尚未完成，可续传"),
        )
    except Exception as error:
        details = runtime_failure_details(error, "download", identifier)
        store.finish(identifier, "failed", error=details["summary"], error_details=details)


def run_financial_download_task(store, task, data_root):
    identifier = task["id"]
    execution = task["execution"]
    try:
        store.stage(identifier, "download", "按股票下载并核对财务报表", 0)

        result = financial_download.run(data_root, execution["plan_id"])
        succeeded = result["status"] == "complete"
        store.finish(
            identifier,
            "succeeded" if succeeded else "interrupted",
            result={
                key: result[key]
                for key in ("id", "status", "version_id", "completed", "total", "skipped", "message")
                if key in result
            },
            error=None if succeeded else result.get("message", "财务数据尚未完整，请查看下载明细后续传"),
        )
    except Exception as error:
        details = runtime_failure_details(error, "download", identifier)
        store.finish(identifier, "failed", error=details["summary"], error_details=details)


def run_industry_download_task(store, task):
    identifier = task["id"]
    execution = task["execution"]
    try:
        store.stage(identifier, "download", "下载申万行业分类与历史成分", 0)

        def progress(row):
            store.stage(identifier, "download", row.get("message", "下载行业数据"), row.get("completed", 0))

        result = industry_download.run(execution["root"], execution["plan_id"], on_progress=progress)
        succeeded = result["status"] == "complete"
        store.finish(
            identifier,
            "succeeded" if succeeded else "interrupted",
            result={
                key: result[key]
                for key in (
                    "id",
                    "status",
                    "version_id",
                    "completed",
                    "total",
                    "levels",
                    "message",
                    "empty_sectors",
                    "excluded_members",
                )
                if key in result
            },
            error=None if succeeded else result.get("message", "行业数据尚未完整，请查看下载明细后续传"),
        )
    except Exception as error:
        details = runtime_failure_details(error, "download", identifier)
        store.finish(identifier, "failed", error=details["summary"], error_details=details)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs-root", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--storage-root", type=Path, required=True)
    args = parser.parse_args()

    configure_store(PostgresMarketStore())
    runs_root, storage_root = args.runs_root.resolve(), args.storage_root.resolve()
    run_task(
        runs_root,
        args.data_root.resolve(),
        args.task_id,
        task_store=PgTaskStore(runs_root),
        market_repository=MarketRepository(),
        storage_root=storage_root,
        result_store_factory=lambda dataset_id=None: PgResultStore(
            runs_root, storage_root=storage_root, dataset_id=dataset_id
        ),
    )


if __name__ == "__main__":
    main()
