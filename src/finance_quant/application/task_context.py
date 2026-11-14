"""Execution context and frozen dataset preparation, independent of worker dispatch."""

from pathlib import Path

from finance_quant.application import market_download
from finance_quant.application.research_requests import project_snapshot
from finance_quant.checksums import file_sha256
from finance_quant.configuration import DataConfig
from finance_quant.data.financials import FinancialBinding, financial_root
from finance_quant.data.market import load_market
from finance_quant.data.market_universe import market_roots
from finance_quant.data.validation_config import CrosscheckConfig


class TaskContext:
    """Collaborators shared by research and experiment execution."""

    def __init__(self, identifier, store, runs_root, data_root, storage_root, result_store_factory, repository):
        self.identifier, self.store = identifier, store
        self.runs_root, self.data_root, self.storage_root = runs_root, data_root, storage_root
        self.result_store_factory, self.repository = result_store_factory, repository

    def stage(self, phase, message, completed):
        self.store.stage(self.identifier, phase, message, completed)


def register_snapshot_dataset(root, request, repository, storage_root):
    """Register an immutable dataset when a PostgreSQL repository was injected."""
    if repository is None:
        return None
    return repository.register_snapshot(root, storage_root, request=request.get("data", {}))


def attach_outputs(result_store, dataset_id, result):
    if not dataset_id:
        return
    attach = getattr(result_store, "attach_dataset", None)
    if attach is None:
        return
    for category, key in (
        ("factors", "factor_run_id"),
        ("backtests", "backtest_run_id"),
        ("evaluations", "evaluation_run_id"),
    ):
        attach(category, result[key], dataset_id)


def financial_binding(execution: dict, data_root):

    pinned = execution.get("financials", {}).get("version")
    return FinancialBinding.from_dict(financial_root(data_root), execution["financials"]) if pinned else None


def prepare_snapshot(context: TaskContext, request: dict, execution: dict):
    """Pin the queued snapshot version and project the requested stock pool."""
    snapshot_dir = Path(execution["snapshot_dir"])
    if not snapshot_dir.resolve().is_relative_to(context.data_root.resolve()):
        raise ValueError("Invalid snapshot location")
    parent = load_market(snapshot_dir)[3]
    if execution.get("manifest_sha256") and file_sha256(snapshot_dir / "manifest.json") != execution["manifest_sha256"]:
        raise ValueError("排队期间股票池历史状态发生变化，请刷新数据列表后重新提交")
    if parent["id"] != execution["data_version"] or parent["files"] != execution["files"]:
        raise ValueError("排队期间数据版本发生变化，请刷新数据列表后重新提交")
    root = project_snapshot(snapshot_dir, tuple(request["data"]["codes"]), context.data_root)
    market, _, calendar, manifest = load_market(root)
    data = DataConfig(
        root,
        manifest["start"],
        manifest["end"],
        manifest["codes"],
        manifest.get("benchmark", "000300.SH"),
        CrosscheckConfig(manifest.get("cross_validation", {}).get("mode", "off")),
    )
    dataset_record = register_snapshot_dataset(root, request, context.repository, context.storage_root)
    return market, calendar, manifest, data, dataset_record


def prepare_warehouse(context: TaskContext, request: dict, execution: dict):
    """Freeze a queued dynamic pool once so every split shares the same physical files."""
    root = Path(execution["warehouse_dir"]).resolve()
    allowed = (
        market_roots(context.data_root)
        if execution["mode"] == "market_warehouse"
        else market_download.warehouse_roots(context.data_root)
    )
    if root not in allowed:
        raise ValueError("历史数据仓路径不合法")
    dataset_record = None
    if context.repository is not None:
        dataset_record = context.repository.freeze_historical_warehouse(
            root, context.storage_root, expected_sha=execution["manifest_sha256"], request=request.get("data", {})
        )
        root = Path(dataset_record["path"])
    return root, dataset_record
