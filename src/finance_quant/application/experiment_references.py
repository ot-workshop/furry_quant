"""Small submission pins and worker-side checks for experiment reference data."""

from __future__ import annotations

import json
import re
from itertools import chain
from pathlib import Path
from stat import S_ISREG

from finance_quant.checksums import file_sha256
from finance_quant.data.financials import FinancialBinding, FinancialStore, financial_root
from finance_quant.data.industries import industry_root
from finance_quant.research.factor_library import requires_financials, uses_financials
from finance_quant.research.neutralization import parse_neutralization

REFERENCE_KEY = "reference_versions"
_POINTER_LIMIT = 4096


def _pointer(root: Path, pattern: str, label: str) -> str | None:
    pointer = root / "current.json"
    try:
        if not S_ISREG(pointer.lstat().st_mode):
            raise ValueError(f"{label}版本指针不是普通文件，请检查数据管理")
        with pointer.open("rb") as stream:
            raw = stream.read(_POINTER_LIMIT + 1)
    except FileNotFoundError:
        return None
    except OSError:
        raise ValueError(f"无法读取{label}版本指针，请检查数据管理") from None
    try:
        value = json.loads(raw) if len(raw) <= _POINTER_LIMIT else None
    except (ValueError, RecursionError):
        value = None
    version = value.get("id") if isinstance(value, dict) else None
    if not isinstance(version, str) or re.fullmatch(pattern, version) is None:
        raise ValueError(f"{label}版本指针无效，请检查数据管理")
    return version


def capture_experiment_references(document, plan, data_root) -> dict:
    """Prepare private pins; explicit null means current was absent, never select it later."""
    if document.get("factor_selection") is not None:
        # Its immutable evaluation/factor evidence owns these versions, not global current.
        return {"source": "selection"}
    data = document["data"]
    filters = data.get("filters", {}) if isinstance(data, dict) else {}
    filter_version = (
        filters.get("industry_version") if isinstance(filters, dict) and filters.get("industries") else None
    )
    need_filter = isinstance(filters, dict) and bool(filters.get("industries"))
    requested = document.get("neutralization")
    explicit = requested.get("industry_version") if isinstance(requested, dict) else None
    candidate_versions = {
        item.neutralization.industry_version
        for item in plan.candidates
        if item.neutralization and item.neutralization.industry_version
    }
    if len(candidate_versions) > 1 or (explicit and candidate_versions - {explicit}):
        raise ValueError("各方案必须共用同一历史行业版本")
    industry = explicit or next(iter(candidate_versions), None)
    # Both implicit lookups share a single observed pointer, but explicitly configured
    # filter and neutralization versions remain separate.
    if not industry and filter_version and (not isinstance(requested, dict) or requested.get("mode", "off") == "off"):
        industry = filter_version
    if industry is None or (need_filter and filter_version is None):
        latest = _pointer(industry_root(), r"[0-9a-f]{64}", "历史行业")
        if industry is None:
            industry = latest
        if need_filter and filter_version is None:
            filter_version = latest
    return {
        "source": "submission",
        "financial": (
            _pointer(financial_root(data_root), r"[0-9a-f]{24}", "财务")
            if any(uses_financials(item.factors) for item in plan.candidates)
            else None
        ),
        "industry": industry,
        "filter_industry": filter_version,
    }


def pin_industry_document(research: dict, industry_version: str | None, filter_version: str | None) -> dict:
    """Return a worker copy, leaving the public submission and request fingerprint intact."""
    neutral = parse_neutralization(research.get("neutralization"))
    if industry_version is not None:
        research = {**research, "neutralization": {**neutral.as_dict(), "industry_version": industry_version}}
    data = research["data"]
    filters = data.get("filters")
    if isinstance(filters, dict) and filters.get("industries"):
        if filter_version is None:
            raise ValueError("提交时未有可用历史行业版本，不能执行行业筛选")
        research = {**research, "data": {**data, "filters": {**filters, "industry_version": filter_version}}}
    return research


def verify_financial_version(binding: FinancialBinding | None, plan, data: dict) -> None:
    """Check the pinned manifest and files for the declared universe before any writes."""
    configs = [item.factors for item in plan.candidates]
    if binding is None:
        if any(requires_financials(config) for config in configs):
            raise ValueError("提交时未有可用财务版本，请下载财务数据后重新提交")
        return
    try:
        manifest = FinancialStore(binding.root).version(binding.version)
        codes = data.get("codes")
        stocks = manifest["stocks"]
        entries = stocks.values() if codes is None else (stocks[c] for c in set(codes) if c in stocks)
        for entry in chain((manifest["calendar"],), entries):
            identifier = entry["object"]
            if not isinstance(identifier, str) or re.fullmatch(r"[0-9a-f]{64}", identifier) is None:
                raise ValueError("Invalid financial object")
            if file_sha256(binding.root / "objects" / f"{identifier}.parquet") != entry["sha256"]:
                raise ValueError("Financial object checksum mismatch")
    except (OSError, ValueError, TypeError, KeyError):
        raise ValueError("提交时绑定的财务版本已缺失或损坏，请检查数据后重新提交") from None
