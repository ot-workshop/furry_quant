"""Health, data inventory and research overview routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy.exc import SQLAlchemyError

from finance_quant.application import market_download
from finance_quant.application.research_requests import snapshot_registry
from finance_quant.data.historical_warehouse import warehouse_option
from finance_quant.data.market_universe import market_option, market_roots

SYNTHETIC_NOTICE = "合成样本，仅验证系统流程，不代表真实股票表现"


def research_snapshots(data_root) -> list[dict]:
    """Selectable research datasets with their coverage evidence, newest first."""

    items = []
    for identifier, (_, _, _, manifest) in snapshot_registry(data_root).items():
        coverage = manifest.get("coverage", {})
        items.append(
            {
                "id": identifier,
                "source": manifest.get("source"),
                "schema_version": manifest.get("schema_version"),
                "start": manifest["start"],
                "end": manifest["end"],
                "codes": manifest["codes"],
                "benchmark": manifest.get("benchmark"),
                "trading_days": manifest.get("trading_days"),
                "rows": manifest.get("rows"),
                "is_subset": "projection" in manifest,
                "parent_id": manifest.get("projection", {}).get("parent_data_version"),
                "shared_version": manifest.get("shared_version"),
                "validation_counts": manifest.get("validation_counts"),
                "coverage": {
                    key: coverage.get(key)
                    for key in ("status", "expected_sessions", "stock_count", "stocks", "benchmark", "note")
                },
                "cross_validation": manifest.get("cross_validation"),
                "notes": manifest.get("notes", []),
            }
        )
    items.sort(key=lambda row: (row["is_subset"], row["end"]), reverse=False)
    return items


def historical_indices(data_root) -> list[dict]:
    """Published CSI 300 dynamic-universe versions, newest first, without the per-day calendar."""

    items = []
    for root in market_download.warehouse_roots(data_root):
        option = warehouse_option(root)
        if option:
            dates = option.pop("trading_dates")
            items.append({**option, "trading_days": len(dates)})
    return items


def system_router(store, data_root, *, database_probe=None, market_status=None) -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    @router.get("/health")
    def health():
        database = {}
        if database_probe is not None:
            try:
                database = database_probe()
            except Exception:
                raise HTTPException(503, "数据库连接或版本检查未通过") from None
        return {"status": "ok", "read_only": False, "task_execution": "postgres_worker", **database}

    @router.get("/data-status")
    def data_status():
        try:
            return {
                "snapshots": research_snapshots(data_root),
                "historical_indices": historical_indices(data_root),
                "market_universes": [
                    {
                        **{key: value for key, value in option.items() if key != "trading_dates"},
                        "trading_days": len(option["trading_dates"]),
                    }
                    for root in market_roots(data_root)
                    if (option := market_option(root))
                ],
                "database": market_status() if market_status is not None else None,
            }
        except (SQLAlchemyError, OSError, ValueError):
            raise HTTPException(503, "数据状态暂时无法读取，请检查数据库连接与存储目录") from None

    @router.get("/overview")
    def overview():
        factors, unavailable = store.list_metadata("factors")
        backtests, bad_backtests = store.list_metadata("backtests")
        _, bad_evaluations = store.list_metadata("evaluations")
        unavailable += bad_backtests + bad_evaluations
        instruments = {row["instrument"] for result in factors for row in result["metadata"]["universe_members"]}
        modes = {bool(result["metadata"]["is_synthetic"]) for result in factors}
        modes.update(bool(result["summary"]["is_synthetic"]) for result in backtests)
        if len(modes) > 1:
            mode = "mixed"
        elif modes == {True}:
            mode = "synthetic"
        elif modes == {False}:
            mode = "real"
        else:
            mode = "empty"
        notices = {
            "synthetic": SYNTHETIC_NOTICE,
            "real": "真实行情按批次保存；请查看所选批次的数据来源、校验等级与模拟假设",
            "mixed": "真实行情与合成样本分别保存；请以所选评分批次和回测的数据标识为准",
            "empty": "尚无研究结果",
        }
        notice = notices[mode]
        if unavailable:
            notice += f"；有{len(unavailable)}条结果暂不可读，已隔离显示，其他结果仍可查看"
        return {
            "mode": mode,
            "notice": notice,
            "factor_runs": len(factors),
            "backtests": len(backtests),
            "instruments": len(instruments),
            **({"unavailable_results": unavailable} if unavailable else {}),
        }

    return router
