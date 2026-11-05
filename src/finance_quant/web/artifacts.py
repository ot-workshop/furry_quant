"""Shared result lookup and response helpers for HTTP routes."""

from __future__ import annotations

from fastapi import HTTPException


class ArtifactReader:
    """Validate stored artifacts once before route-specific projection."""

    def __init__(self, store):
        self.store = store

    def get(self, category: str, identifier: str) -> dict:
        try:
            result = self.store.get(category, identifier, hydrate=False)
            if not isinstance(result, dict) or not isinstance(result.get("metadata"), dict):
                raise ValueError("Invalid artifact")
            if category == "backtests" and not isinstance(result.get("summary"), dict):
                raise ValueError("Invalid backtest")
            target = "summary" if category == "backtests" else "metadata"
            result[target] = {**result[target], "id": identifier}
            return result
        except FileNotFoundError:
            raise HTTPException(404, "结果不存在，请先生成研究结果") from None
        except ValueError:
            try:
                exists = self.store.exists(category, identifier)
            except (FileNotFoundError, ValueError):
                exists = False
            detail = "结果文件损坏或格式异常，请重新运行该配置" if exists else "结果不存在，请先生成研究结果"
            raise HTTPException(409 if exists else 404, detail) from None


def page(items: list, offset: int, limit: int) -> dict:
    """Return the stable pagination envelope used by result tables."""
    return {
        "items": items[offset : offset + limit],
        "total": len(items),
        "offset": offset,
        "limit": limit,
    }


def factor_metadata(result: dict) -> dict:
    """Remove large date-indexed diagnostics from factor list responses."""
    metadata = dict(result["metadata"])
    metadata.pop("industry_coverage_by_date", None)
    if result.get("tables"):
        for key in [
            "membership_by_execution_date",
            "coverage_by_date",
            "availability_by_date",
            "lifecycle_exclusions",
            "industry_coverage_by_date",
        ]:
            metadata.pop(key, None)
    return metadata
