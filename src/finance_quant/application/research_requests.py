"""Web research requests: strict parameters, pinned snapshots and stock subsets."""

from __future__ import annotations

import copy
import json
import re
import shutil
import tempfile
from dataclasses import asdict, replace
from datetime import datetime
from os import scandir
from pathlib import Path
from stat import S_ISDIR, S_ISREG
from zoneinfo import ZoneInfo

import pandas as pd
from filelock import FileLock

from finance_quant.application import market_download
from finance_quant.application.market_requests import (
    pool_industry_display,
    validate_market_request,
    validate_pool_filters,
)
from finance_quant.application.research_readiness import validate_warmup
from finance_quant.checksums import file_sha256, sha256_bytes, sha256_text
from finance_quant.configuration import BacktestConfig, DataConfig, ResearchConfig
from finance_quant.data.benchmarks import benchmark_options
from finance_quant.data.execution_universe import validate_execution_universe
from finance_quant.data.financials import FinancialBinding, FinancialStore, covered_codes, financial_root
from finance_quant.data.historical_warehouse import warehouse_header, warehouse_option
from finance_quant.data.industries import optional_industries
from finance_quant.data.managed import atomic_json, coverage_report
from finance_quant.data.market import FrozenMarketSnapshot, load_market
from finance_quant.data.market_universe import market_option, market_roots
from finance_quant.data.pool_inputs import security_coverage
from finance_quant.data.shared_market import validation_counts
from finance_quant.data.symbols import to_qlib
from finance_quant.data.validation_config import CrosscheckConfig
from finance_quant.research.factor_library import FACTOR_LIBRARY, requires_financials, uses_financials
from finance_quant.research.fundamentals import fundamentals_for
from finance_quant.research.market_factors import market_factors
from finance_quant.research.neutralization import industry_context, parse_neutralization
from finance_quant.research.registry import EvaluationConfig, FactorConfig, FactorSpec
from finance_quant.research.universe import UniverseFilters

MAX_CODES = 50
BENCHMARKS = [
    {"code": "000300.SH", "name": "沪深300"},
    {"code": "000905.SH", "name": "中证500"},
    {"code": "000852.SH", "name": "中证1000"},
    {"code": "000001.SH", "name": "上证指数"},
    {"code": "399001.SZ", "name": "深证成指"},
    {"code": "399006.SZ", "name": "创业板指"},
]
FACTOR_DEFINITIONS = [definition.public() for definition in FACTOR_LIBRARY.values()]


def _fields(value, required, optional=(), label="配置"):
    if not isinstance(value, dict) or not set(required).issubset(value) or set(value) - set(required) - set(optional):
        raise ValueError(f"{label}字段不完整或包含不支持的参数")
    return value


def _iso(value):
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def _backtest_dict(config):
    result = asdict(config)
    if result.get("benchmark") is None:
        result.pop("benchmark", None)
    return result


def lookup_snapshot_meta(data_root: Path, snapshot_id: str) -> tuple[Path, dict, str]:
    """Bind one published leaf without opening market data or following filesystem aliases."""
    matches = _find_snapshot_metas(data_root, snapshot_id)
    if snapshot_id not in matches:
        raise ValueError(f"未找到可用的数据快照：{snapshot_id}")
    return matches[snapshot_id]


def lookup_label_snapshot_metas(data_root: Path) -> list[tuple[Path, dict, str]]:
    """Find unambiguous managed leaves that advertise a label capture."""
    return list(_find_snapshot_metas(data_root, None).values())


def _find_snapshot_metas(data_root: Path, snapshot_id: str | None) -> dict[str, tuple[Path, dict, str]]:

    # Manifest metadata includes security history, not the actual market rows.
    max_manifest_bytes = 1024 * 1024
    bytes_left = 32 * max_manifest_bytes
    entries_left = 4096

    def mode(path):
        try:
            return path.lstat().st_mode
        except FileNotFoundError:
            return None

    def require_direct(path):
        if path.resolve(strict=True) != path:
            raise ValueError("数据快照路径必须位于受管目录内，且不能是链接")

    def directories(parent):
        nonlocal entries_left
        parent_mode = mode(parent)
        if parent_mode is None:
            return
        require_direct(parent)
        if not S_ISDIR(parent_mode):
            raise ValueError("数据快照路径不是目录")
        # scandir streams entries; stop rather than silently scan a partial inventory.
        with scandir(parent) as entries:
            for entry in entries:
                entries_left -= 1
                if entries_left < 0:
                    raise ValueError("数据快照目录超过扫描上限，无法确认快照唯一")
                if entry.name.startswith("."):
                    continue  # Publisher staging directories are not snapshots.
                path = parent / entry.name
                if entry.is_dir(follow_symlinks=False) or entry.is_symlink():
                    require_direct(path)
                    if not entry.is_dir(follow_symlinks=False):
                        raise ValueError("数据快照路径不能是链接")
                    yield path

    def candidates(research):
        yield from directories(research / "subsets")
        for batch in directories(research / "downloads"):
            yield from directories(batch / "snapshots")

    def unique_fields(pairs):
        fields = {}
        for key, value in pairs:
            if key in fields:
                raise ValueError("Duplicate manifest key")
            fields[key] = value
        return fields

    matches: dict[str, tuple[Path, dict, str]] = {}
    seen: set[str] = set()
    try:
        root = Path(data_root).resolve()
        research = root / "research"
        if mode(research) is not None:
            require_direct(research)
        for leaf in candidates(research):
            manifest_path = leaf / "manifest.json"
            manifest_mode = mode(manifest_path)
            if manifest_mode is None:
                continue  # A leaf without its atomically published manifest is not yet visible.
            require_direct(manifest_path)
            if not S_ISREG(manifest_mode):
                raise ValueError("数据快照清单路径不是普通文件")
            with manifest_path.open("rb") as manifest_file:
                payload = manifest_file.read(min(max_manifest_bytes, bytes_left) + 1)
            if len(payload) > max_manifest_bytes:
                raise ValueError("数据快照清单超过大小上限")
            bytes_left -= len(payload)
            if bytes_left < 0:
                raise ValueError("数据快照清单超过读取总量上限，无法确认快照唯一")
            try:
                manifest = json.loads(payload, object_pairs_hook=unique_fields)
            except (ValueError, RecursionError) as error:
                raise ValueError("无法读取数据快照清单，无法确认快照唯一") from error
            if not isinstance(manifest, dict) or not isinstance(manifest.get("id"), str) or not manifest["id"]:
                raise ValueError("无法读取数据快照清单标识，无法确认快照唯一")
            identifier = manifest["id"]
            if snapshot_id is not None and identifier != snapshot_id:
                continue
            if snapshot_id is None:
                if identifier in seen:
                    matches.pop(identifier, None)
                    continue
                seen.add(identifier)
                if "label_capture" not in manifest:
                    continue
            if mode(leaf / "rejected.json") is not None:
                if snapshot_id is None:
                    continue
                raise ValueError(f"数据快照已被隔离：{snapshot_id}")
            if identifier in matches:
                raise ValueError(f"数据快照标识重复：{snapshot_id}")
            if "snapshot_path" in manifest and manifest["snapshot_path"] != f"snapshots/{leaf.name}":
                if snapshot_id is None:
                    continue
                raise ValueError("数据快照清单指向其他路径")
            files = manifest.get("files")
            if not isinstance(files, dict) or set(files) != {"market.parquet", "benchmark.parquet", "calendar.parquet"}:
                if snapshot_id is None:
                    continue
                raise ValueError("数据快照文件摘要不完整")
            if any(
                not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                for digest in files.values()
            ):
                if snapshot_id is None:
                    continue
                raise ValueError("数据快照文件摘要无效")
            matches[identifier] = leaf.relative_to(root), manifest, sha256_bytes(payload)
    except (OSError, RuntimeError) as error:
        # Missing published manifests are handled explicitly; any other I/O failure
        # means the scan cannot prove uniqueness, even if it already saw a match.
        raise ValueError("无法读取数据快照目录或清单，无法确认快照唯一") from error
    return matches


def snapshot_registry(data_root: Path) -> dict:
    """Only published, valid snapshots below server-controlled locations are selectable.

    ``research/downloads`` holds fixed-pool exports of the shared market download;
    ``research/subsets`` holds stock-pool projections created for submitted research.
    """
    root = data_root.resolve()
    paths = [
        *sorted(root.glob("research/downloads/*/snapshots/*")),
        *sorted(root.glob("research/subsets/*")),
    ]
    found = {}
    for path in paths:
        try:
            if not path.resolve().is_relative_to(root) or not (path / "manifest.json").is_file():
                continue
            market, benchmark, calendar, manifest = load_market(path)
            identifier = manifest["id"]
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", identifier):
                continue
            found.setdefault(identifier, (market, benchmark, calendar, manifest))
        except (OSError, ValueError, KeyError, TypeError):
            # Quarantined, partially published and corrupt data is never offered.
            continue
    return found


def research_options(
    data_root: Path,
    runs_root: Path,
    *,
    industry_version=None,
    financial_version=None,
    allow_latest_references=True,
    frozen_snapshot: FrozenMarketSnapshot | None = None,
) -> dict:
    registry = (
        snapshot_registry(data_root)
        if frozen_snapshot is None
        else {frozen_snapshot.inputs[3]["id"]: frozen_snapshot.inputs}
    )
    snapshots = []
    for identifier, (market, _, calendar, manifest) in registry.items():
        names = market.drop_duplicates("instrument").set_index("instrument")["name"].to_dict()
        codes = [{"code": code, "name": names.get(to_qlib(code), code)} for code in sorted(manifest["codes"])]
        source_label = (
            "官方共享行情"
            if manifest.get("source") == "official_tushare_shared"
            else "联合数据"
            if manifest.get("source") == "tushare_baostock"
            else "Tushare 数据"
        )
        snapshots.append(
            {
                "id": identifier,
                "label": f"{source_label} · {len(codes)}只 · {_iso(manifest['start'])} 至 {_iso(manifest['end'])}",
                "start": _iso(manifest["start"]),
                "end": _iso(manifest["end"]),
                "trading_days": manifest["trading_days"],
                "trading_dates": calendar.loc[
                    calendar.is_open
                    & calendar.date.between(pd.Timestamp(manifest["start"]), pd.Timestamp(manifest["end"])),
                    "date",
                ]
                .dt.strftime("%Y-%m-%d")
                .tolist(),
                "benchmark": manifest.get("benchmark", "000300.SH"),
                "is_subset": "projection" in manifest,
                "codes": codes,
                **security_coverage(manifest["snapshot_dir"], manifest, sorted(manifest["codes"])),
                "validation_mode": manifest.get("cross_validation", {}).get("mode", "off"),
                "source": manifest.get("source", "third_party_tushare_compatible"),
            }
        )
    # Shared exports are listed before derived subsets; the first export is the default.
    snapshots.sort(key=lambda row: row["is_subset"])
    selected = snapshots[0] if snapshots else None
    end = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    data = (
        {"mode": "snapshot", "snapshot_id": selected["id"], "codes": [c["code"] for c in selected["codes"]]}
        if selected
        else {"mode": "snapshot", "snapshot_id": None, "codes": []}
    )

    # A fixed-pool selection handoff must validate its source against the pinned
    # snapshot only. Interactive options still enumerate all selectable datasets.
    historical_indices = []
    market_universes = []
    if frozen_snapshot is None:
        historical_indices = [
            option for root in market_download.warehouse_roots(data_root) if (option := warehouse_option(root))
        ]
        market_universes = [option for root in market_roots(data_root) if (option := market_option(root))]
    if not selected and historical_indices:
        # Only a CSI 300 version exists: default to its newest research range.
        latest = historical_indices[0]
        end = latest["end"]
        start = max(latest["start"], str(int(end[:4]) - 1) + "-03-03")
        start = next((day for day in latest["trading_dates"] if day >= start), latest["start"])
    elif selected:
        calendar = registry[selected["id"]][2]
        days = pd.DatetimeIndex(
            calendar.loc[
                calendar.is_open
                & calendar.date.between(pd.Timestamp(selected["start"]), pd.Timestamp(selected["end"])),
                "date",
            ]
        )
        start = days[min(21, len(days) - 1)].strftime("%Y-%m-%d")
        end = selected["end"]
    elif market_universes:
        latest = market_universes[0]
        end = latest["end"]
        history = max(spec.lookback for spec in FactorConfig().specs) + 1
        start = latest["trading_dates"][min(history, len(latest["trading_dates"]) - 1)]
        start = max(start, latest["start"])
        data = {"mode": "market_warehouse", "warehouse_id": latest["id"], "filters": UniverseFilters().as_dict()}
    else:
        start = str(int(end[:4]) - 1) + "-03-03"
    backtest = BacktestConfig(start, end, topk=min(3, len(data.get("codes", [])) or 3))
    factors, evaluation = FactorConfig(), EvaluationConfig()
    neutralization = parse_neutralization()

    industry = optional_industries(industry_version) if industry_version or allow_latest_references else None
    financial = (
        financial_summary(data_root, version=financial_version)
        if financial_version or allow_latest_references
        else None
    )
    return {
        "snapshots": [
            {**row, "financial_coverage": coverage_row(financial, [code["code"] for code in row["codes"]], data_root)}
            for row in snapshots
        ],
        "historical_indices": [
            {**row, "financial_coverage": coverage_row(financial, row["codes"], data_root)}
            for row in historical_indices
        ],
        "market_universes": [
            {**row, "financial_coverage": coverage_row(financial, row["codes"], data_root)} for row in market_universes
        ],
        "financial_data": financial,
        "comparison_benchmarks": benchmark_options(),
        "industry_data": (
            {
                **{
                    k: industry.manifest[k]
                    for k in ["id", "source", "rows", "instruments", "policy", "point_in_time_certified"]
                },
                "data_asof": industry.data_asof,
                "levels": industry.levels,
                "sectors": industry.frame[["level", "industry_code", "industry_name"]]
                .drop_duplicates()
                .sort_values(["level", "industry_code"])
                .to_dict("records"),
            }
            if industry
            else None
        ),
        "factor_definitions": FACTOR_DEFINITIONS,
        "benchmarks": BENCHMARKS,
        "defaults": {
            "name": "我的研究",
            "data": data,
            "backtest": _backtest_dict(backtest),
            "factors": factors.as_list(),
            "evaluation": evaluation.as_dict(),
            **({"neutralization": neutralization.as_dict()} if neutralization.as_dict() != {"mode": "off"} else {}),
        },
        "limits": {"max_codes": MAX_CODES, "max_queued_tasks": 20},
    }


def financial_summary(data_root: Path, *, version=None) -> dict | None:
    """Current financial version, described for the research form and the data page."""

    store = FinancialStore(financial_root(data_root))
    try:
        manifest = store.version(version) if version else store.current()
    except (OSError, ValueError):
        return None
    if manifest is None:
        return None
    stocks = manifest["stocks"]
    periods = sorted(entry["latest_period"] for entry in stocks.values() if entry["latest_period"])
    return {
        "version": manifest["id"],
        "created_at": manifest.get("created_at"),
        "codes": len(stocks),
        "rows": sum(entry["rows"] for entry in stocks.values()),
        "quarantined": sum(entry["quarantined"] for entry in stocks.values()),
        "latest_report_period": periods[-1] if periods else None,
        "earliest_report_period": periods[0] if periods else None,
        "calendar_start": manifest["calendar"]["start"],
        "calendar_end": manifest["calendar"]["end"],
        "policy": manifest["policy"],
        "point_in_time_certified": False,
    }


def coverage_row(financial: dict | None, codes, data_root: Path) -> dict | None:
    """How many of these codes hold a usable report in the current financial version."""
    if financial is None:
        return None

    try:
        manifest = FinancialStore(financial_root(data_root)).version(financial["version"])
    except (OSError, ValueError, KeyError):
        return {"covered": 0, "total": len(set(codes))}
    return {"covered": len(covered_codes(manifest, codes)), "total": len(set(codes))}


def validate_request(
    document: dict,
    data_root: Path,
    runs_root: Path,
    *,
    financials=None,
    references_frozen=False,
    frozen_snapshot: FrozenMarketSnapshot | None = None,
) -> tuple[dict, dict]:
    _fields(
        document,
        ("name", "data", "backtest", "factors", "evaluation"),
        ("neutralization", "financial_policy"),
        label="研究",
    )
    neutralization = parse_neutralization(document.get("neutralization"))
    if references_frozen:
        neutralization = replace(neutralization, allow_latest=False)
    industry = industry_context(neutralization) if "neutralization" in document else None
    neutral_extra = {}
    if "neutralization" in document:
        neutral_extra = {
            "neutralization": {
                **neutralization.as_dict(),
                **({"industry_version": industry.manifest["id"]} if industry else {}),
            }
        }
        neutralization = replace(
            neutralization, industry_version=neutral_extra["neutralization"].get("industry_version")
        )
    name = document["name"]
    if not isinstance(name, str) or not name.strip() or len(name) > 80 or any(ord(c) < 32 for c in name):
        raise ValueError("研究名称须为1至80个字符，不含换行")
    _fields(document["backtest"], ("start", "end"), tuple(BacktestConfig.__dataclass_fields__)[2:], "回测")
    backtest = BacktestConfig(**document["backtest"])
    if neutralization.mode == "industry":
        industry.validate_range(backtest.end)
    _fields(document["evaluation"], (), ("horizons", "groups", "min_cross_section"), "因子评估")
    evaluation = EvaluationConfig(**document["evaluation"])
    factor_list = document["factors"]
    if not isinstance(factor_list, list):
        raise ValueError("请选择因子并设置权重")
    factors = FactorConfig(
        tuple(
            FactorSpec(
                **_fields(
                    item, ("name", "weight"), ("window", "skip", "baseline_window", "id", "missing_policy"), "因子"
                )
            )
            for item in factor_list
        )
    )
    binding = financials if financials is not None or references_frozen else pin_financials(factors, data_root)
    if any(spec.name == "gross_margin_ttm" for spec in factors.specs) and not neutralization.industry_version:
        industry = industry_context(neutralization)
        if industry:
            neutral_extra = {
                "neutralization": {**neutralization.as_dict(), "industry_version": industry.manifest["id"]}
            }
            neutralization = replace(neutralization, industry_version=industry.manifest["id"])
    financial_extra = {"financials": binding.as_dict()} if binding else {}
    obj = document["data"]
    if not isinstance(obj, dict) or obj.get("mode") not in {"snapshot", "index_warehouse", "market_warehouse"}:
        raise ValueError("请选择数据集：固定股票池快照或沪深300历史成分仓库")
    if obj["mode"] == "market_warehouse":
        selected, pins, option = validate_market_request(obj, data_root, backtest, factors, evaluation)
        neutral_extra = pool_industry_display(selected, neutral_extra, neutralization)
        require_financial_coverage(binding, option["codes"], factors)
        if binding:
            pins["financials"] = binding.as_dict()
        return {
            "name": name.strip(),
            "data": selected,
            "backtest": _backtest_dict(backtest),
            "factors": factors.as_list(),
            "evaluation": evaluation.as_dict(),
            **neutral_extra,
            **financial_extra,
        }, pins
    if obj["mode"] == "index_warehouse":
        _fields(obj, ("mode", "warehouse_id"), ("filters",), label="历史成分仓库")

        root = next(
            (
                r
                for r in market_download.warehouse_roots(data_root)
                if (o := warehouse_option(r)) and o["id"] == obj["warehouse_id"]
            ),
            None,
        )
        option = warehouse_option(root) if root is not None else None
        if option is None or obj["warehouse_id"] != option["id"]:
            raise ValueError("沪深300历史成分版本不存在或已变化，请刷新数据列表")
        _, calendar, digest = warehouse_header(root)
        if not option["start"] <= backtest.start <= backtest.end <= option["end"]:
            raise ValueError("回测区间超出历史仓库研究范围")
        sessions = pd.DatetimeIndex(calendar.loc[calendar.is_open, "date"])
        if pd.Timestamp(backtest.start) not in sessions or pd.Timestamp(backtest.end) not in sessions:
            raise ValueError("回测起止日期必须为交易日")
        if ((sessions >= pd.Timestamp(backtest.start)) & (sessions <= pd.Timestamp(backtest.end))).sum() <= max(
            evaluation.horizons
        ):
            raise ValueError("回测交易日数须超过最长评估持有期")
        if backtest.topk > 300 or backtest.initial_cash > 1e12:
            raise ValueError("目标持股最多300只，资金不超过一万亿元")
        validate_warmup(option["data_start"], option["end"], backtest.start, factors, calendar)
        filter_extra = (
            {"filters": validate_pool_filters(obj["filters"], option, backtest, factors, calendar)}
            if "filters" in obj
            else {}
        )
        if filter_extra:
            neutral_extra = pool_industry_display(filter_extra, neutral_extra, neutralization)
        require_financial_coverage(binding, option["codes"], factors)
        normalized = {
            "name": name.strip(),
            "data": {"mode": "index_warehouse", "warehouse_id": option["id"], **filter_extra},
            "backtest": _backtest_dict(backtest),
            "factors": factors.as_list(),
            "evaluation": evaluation.as_dict(),
        }
        normalized.update(neutral_extra)
        normalized.update(financial_extra)
        pins = {"mode": "index_warehouse", "warehouse_dir": str(root.resolve()), "manifest_sha256": digest}
        if binding:
            pins["financials"] = binding.as_dict()
        return normalized, pins
    _fields(obj, ("mode", "snapshot_id", "codes"), ("filters",), label="快照")
    identifier = obj["snapshot_id"]
    if frozen_snapshot is not None:
        market, _, calendar, manifest = frozen_snapshot.inputs
        if identifier != manifest["id"]:
            raise ValueError("请求的快照标识与提交时冻结版本不一致")
    else:
        if not isinstance(identifier, str) or identifier not in (registry := snapshot_registry(data_root)):
            raise ValueError("数据快照不存在或未通过校验，请刷新数据列表")
        market, _, calendar, manifest = registry[identifier]
    data = DataConfig(
        Path(manifest["snapshot_dir"]),
        manifest["start"],
        manifest["end"],
        obj["codes"],
        manifest.get("benchmark", "000300.SH"),
        CrosscheckConfig(manifest.get("cross_validation", {}).get("mode", "off")),
    )
    if not set(data.codes).issubset(manifest["codes"]):
        raise ValueError("所选股票不在该快照中；请先在数据管理中下载包含这些股票的行情")
    execution = {
        "mode": "snapshot",
        "snapshot_dir": str(data.root),
        "data_version": manifest["id"],
        "files": manifest["files"],
        **({"financials": binding.as_dict()} if binding else {}),
    }
    normalized_data = {"mode": "snapshot", "snapshot_id": identifier, "codes": sorted(data.codes)}
    if len(data.codes) > MAX_CODES:
        raise ValueError(f"单次研究最多选择{MAX_CODES}只股票")
    validate_execution_universe(data.codes)
    if backtest.initial_cash > 1e12:
        raise ValueError("初始资金不能超过一万亿元")
    require_financial_coverage(binding, list(data.codes), factors)
    config = ResearchConfig(data, backtest, runs_root, factors, evaluation, neutralization, binding)
    if frozen_snapshot is not None:
        execution["manifest_sha256"] = frozen_snapshot.manifest_sha256
    if "filters" in obj:
        if frozen_snapshot is None:
            execution["manifest_sha256"] = file_sha256(Path(manifest["snapshot_dir"]) / "manifest.json")
        validate_warmup(data.start, data.end, backtest.start, factors, calendar)
        sessions = pd.DatetimeIndex(calendar.loc[calendar.is_open, "date"])
        if (
            pd.Timestamp(backtest.start) not in sessions
            or pd.Timestamp(backtest.end) not in sessions
            or not _iso(manifest["start"]) <= backtest.start <= backtest.end <= _iso(manifest["end"])
        ):
            raise ValueError("回测区间须在所选行情范围内，且起止为交易日")
        if len(sessions[(sessions >= backtest.start) & (sessions <= backtest.end)]) <= max(evaluation.horizons):
            raise ValueError("回测交易日数须超过最长评估持有期")
        option = {
            "data_start": _iso(manifest["start"]),
            **security_coverage(manifest["snapshot_dir"], manifest, list(data.codes)),
        }
        normalized_data["filters"] = validate_pool_filters(obj["filters"], option, backtest, factors, calendar)
        neutral_extra = pool_industry_display(normalized_data, neutral_extra, neutralization)
    else:
        validate_research_data(config, market, calendar)
    return {
        "name": name.strip(),
        "data": normalized_data,
        "backtest": _backtest_dict(backtest),
        "factors": factors.as_list(),
        "evaluation": evaluation.as_dict(),
        **neutral_extra,
        **financial_extra,
    }, execution


def pin_financials(factors: FactorConfig, data_root: Path):
    """Pin the current financial version when any factor needs statements."""

    return _pin_financials(data_root, required=requires_financials(factors)) if uses_financials(factors) else None


def pin_financials_for(configs, data_root: Path):
    """Pin for a set of alternative factor configurations (experiment candidates)."""

    return (
        _pin_financials(data_root, required=any(requires_financials(config) for config in configs))
        if any(uses_financials(config) for config in configs)
        else None
    )


def _pin_financials(data_root: Path, *, required=True):

    root = financial_root(data_root)
    manifest = FinancialStore(root).current()
    if manifest is None:
        if not required:
            return None
        raise ValueError("所选基本面因子需要财务数据：请先在“数据管理 → 财务数据”下载财务数据")
    return FinancialBinding(root, manifest["id"])


def require_financial_coverage(binding, codes, factors=None) -> None:
    """Refuse a request whose universe holds no usable report in the pinned version."""
    if binding is None or (factors is not None and not requires_financials(factors)):
        return

    manifest = FinancialStore(binding.root).version(binding.version)
    covered = covered_codes(manifest, codes)
    if not covered:
        raise ValueError("所选股票在该财务数据版本中没有任何财报，请先更新财务数据或更换股票池")
    if len(covered) < 2:
        raise ValueError(f"该财务数据版本只覆盖 {len(covered)} 只所选股票，不足以进行横截面评分")


def request_config(
    request: dict, data: DataConfig, runs_root: Path, data_root: Path, *, references_frozen=False
) -> ResearchConfig:

    pinned = request.get("financials", {}).get("version")
    binding = FinancialBinding.from_dict(financial_root(data_root), request["financials"]) if pinned else None
    return ResearchConfig(
        data,
        BacktestConfig(**request["backtest"]),
        runs_root,
        FactorConfig(tuple(FactorSpec(**item) for item in request["factors"])),
        EvaluationConfig(**request["evaluation"]),
        replace(parse_neutralization(request.get("neutralization")), allow_latest=not references_frozen),
        binding,
    )


def validate_research_data(config: ResearchConfig, market, calendar) -> None:
    validate_execution_universe(config.data.codes)
    sessions = pd.DatetimeIndex(calendar.loc[calendar.is_open, "date"])
    start, end = pd.Timestamp(config.backtest.start), pd.Timestamp(config.backtest.end)
    if start not in sessions or end not in sessions:
        raise ValueError("回测起止日期必须是实际交易日")
    if end > market.date.max():
        raise ValueError("回测结束日期超出已有行情")
    left, right = sessions.get_loc(start), sessions.get_loc(end)
    if left == 0:
        raise ValueError("回测开始前至少需要一个交易日的评分数据，请延后开始日期")
    if right - left + 1 <= max(config.evaluation.horizons):
        raise ValueError("回测交易日数须超过最长评估持有期；可延长回测或调小评估持有期")
    validate_warmup(config.data.start, config.data.end, config.backtest.start, config.factors, calendar)
    codes = {to_qlib(code) for code in config.data.codes}
    selected = market.loc[market.instrument.isin(codes)]
    try:
        result = market_factors(
            selected,
            "preflight",
            config.factors,
            neutralization=config.neutralization,
            financials=fundamentals_for(config.financials, config.factors, selected),
        )
    except ValueError as error:
        if str(error).startswith(
            ("行业中性化", "历史行业", "所选因子缺少数据字段", "有效评分不足", "因子输入股票池", "所选基本面因子")
        ):
            raise
        raise ValueError("历史预热数据不足或所选因子没有有效值，请缩短窗口或延后回测开始日期") from None
    coverage = result["metadata"]["coverage_by_date"]
    for date in sessions[left - 1 : right].strftime("%Y-%m-%d"):
        required = max(2, config.backtest.topk)
        if coverage.get(date, {}).get("eligible", 0) < required:
            if result["metadata"].get("industry_coverage_by_date", {}).get(date, {}).get("no_residual_variation"):
                raise ValueError(f"{date}行业中性化后无行业内有效区分，请扩大股票池，或关闭中性化")
            detail = result["metadata"]["availability_by_date"][date]
            definitions = result["metadata"]["factor_definitions"]
            missing = "、".join(
                f"{definitions[key]['label']}[{key}] {count['valid']}/{detail['universe']}只"
                for key, count in detail["factors"].items()
                if count["missing"]
            )
            raise ValueError(
                f"{date}有效评分不足：共同有效{detail['eligible']}/{detail['universe']}只，至少需要{required}只"
                f"（目标持股{config.backtest.topk}只）；不足指标：{missing or '共同交集不足'}。"
                "请检查指标公式、历史预热及停牌，或调整股票池、窗口、回测起点和持股数"
            )


def project_snapshot(snapshot_dir: Path, codes: tuple[str, ...], data_root: Path) -> Path:
    """Recompute the scoring universe without altering or advancing the source snapshot."""
    market, benchmark, calendar, parent = load_market(snapshot_dir)
    if set(codes) == set(parent["codes"]):
        return Path(parent["snapshot_dir"])
    if not set(codes).issubset(parent["codes"]):
        raise ValueError("股票池超出父快照")
    projection = {
        "parent_data_version": parent["id"],
        "parent_files": parent["files"],
        "codes": sorted(codes),
        "code_sha256": file_sha256(Path(__file__)),
    }
    digest = sha256_text(json.dumps(projection, sort_keys=True))[:20]
    target = data_root / "research/subsets" / digest
    target.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(target.with_suffix(".lock"), timeout=0):
        if target.exists():
            saved = load_market(target)[3]
            if saved.get("projection") != projection:
                raise ValueError("派生快照与已有内容冲突")
            return target
        chosen = market.loc[market.instrument.isin({to_qlib(code) for code in codes})].copy()
        sessions = pd.DatetimeIndex(
            calendar.loc[
                calendar.is_open & calendar.date.between(pd.Timestamp(parent["start"]), pd.Timestamp(parent["end"])),
                "date",
            ]
        )
        coverage = coverage_report(chosen, benchmark, sessions, codes, parent.get("benchmark", "000300.SH"))
        if coverage["status"] != "complete":
            raise ValueError("所选股票池数据不完整")
        manifest = copy.deepcopy(parent)
        for key in ("snapshot_path", "snapshot_dir"):
            manifest.pop(key, None)
        manifest.update(
            id="subset-" + digest, codes=sorted(codes), rows=len(chosen), coverage=coverage, projection=projection
        )
        if manifest.get("schema_version") == "market-v4-shared":
            manifest["validation_counts"] = validation_counts(chosen)
        if "cross_validation" in manifest:
            report = manifest["cross_validation"]
            report["stocks"] = [s for s in report["stocks"] if s["code"] in codes]
            report["fallback_codes"] = [s["code"] for s in report["stocks"] if s["status"] == "fallback"]
            report["conflict_codes"] = []
            report["status"] = "fallback" if report["fallback_codes"] else "verified"
            coverage["cross_validation"] = report
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=target.parent))
        try:
            chosen.to_parquet(staging / "market.parquet", index=False)
            for name in ("benchmark.parquet", "calendar.parquet"):
                shutil.copyfile(Path(parent["snapshot_dir"]) / name, staging / name)
            manifest["files"] = {
                name: file_sha256(staging / name)
                for name in ("market.parquet", "benchmark.parquet", "calendar.parquet")
            }
            atomic_json(staging / "manifest.json", manifest)
            load_market(staging)
            staging.rename(target)
        finally:
            if staging.exists():
                # Only the three files and manifest created by this call are removed.
                for name in ("market.parquet", "benchmark.parquet", "calendar.parquet", "manifest.json"):
                    (staging / name).unlink(missing_ok=True)
                staging.rmdir()
        return target
