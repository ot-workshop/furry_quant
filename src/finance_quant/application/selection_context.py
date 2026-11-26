"""Immutable selection references and source configuration reconstruction."""

from __future__ import annotations

from dataclasses import asdict

from finance_quant.application.research_requests import research_options
from finance_quant.configuration import BacktestConfig
from finance_quant.data.financials import FinancialBinding, financial_root
from finance_quant.provenance import fingerprint
from finance_quant.research.factor_library import FACTOR_LIBRARY
from finance_quant.research.factor_selection import ALGORITHM, SelectionParameters, select_factors
from finance_quant.research.neutralization import parse_neutralization
from finance_quant.research.registry import FactorConfig, FactorSpec


def selection_result(store, evaluation_id, parameters):
    evaluation = store.get("evaluations", evaluation_id, hydrate=False)
    result = select_factors(evaluation, parameters)
    digest = store.digest("evaluations", evaluation_id)
    identifier = "factor-selection-" + fingerprint(
        {"source": digest, "parameters": parameters.as_dict(), "algorithm": ALGORITHM}
    )
    result.update(
        selection_id=identifier,
        evaluation_id=evaluation_id,
        source_sha256=digest,
        data_version=evaluation["metadata"].get("data_version"),
        reference={"evaluation_id": evaluation_id, "parameters": parameters.as_dict(), "selection_id": identifier},
    )
    return result


def checked_selection(store, reference):
    if store is None:
        raise ValueError("筛选来源读取服务不可用，请刷新后重试")
    if not isinstance(reference, dict) or set(reference) != {"evaluation_id", "parameters", "selection_id"}:
        raise ValueError("组合筛选来源字段不完整")
    if not isinstance(reference["parameters"], dict):
        raise ValueError("筛选参数须为对象")
    try:
        result = selection_result(store, reference["evaluation_id"], SelectionParameters(**reference["parameters"]))
    except FileNotFoundError:
        raise ValueError("来源因子评估不存在，请重新选择评估结果") from None
    if result["selection_id"] != reference["selection_id"]:
        raise ValueError("来源评估或筛选算法已变化，请重新筛选组合")
    if result["status"] != "optimal":
        raise ValueError("当前筛选没有可用于比较的组合")
    return result


def factor_configs(evaluation, selection):
    rows = evaluation["metadata"].get("factor_config")
    if not rows:
        raise ValueError("该评估未保存因子参数，无法自动创建比较；原结果仍可查看")
    definitions = evaluation["metadata"].get("factor_definitions", {})
    if any(
        row["name"] == "value"
        and definitions.get(row.get("id", row["name"]), {}).get("version")
        != FACTOR_LIBRARY["value"].public()["version"]
        for row in rows
    ):
        raise ValueError("来源盈利收益率为旧PE口径，请按当前TTM利润口径重新评估后创建比较；旧结果仍可查看")
    if any(
        row["name"] == "gross_margin_ttm"
        and definitions.get(row.get("id", row["name"]), {}).get("version")
        != FACTOR_LIBRARY["gross_margin_ttm"].public()["version"]
        for row in rows
    ):
        raise ValueError("来源毛利率为旧收入或适用性口径，请重新评估后创建比较；旧结果仍可查看")
    baseline = FactorConfig(tuple(FactorSpec(**row) for row in rows))
    chosen = set(selection["selected_keys"])
    selected = FactorConfig(
        tuple(
            FactorSpec(**{**spec.as_dict(), "weight": 1 / len(chosen)}) for spec in baseline.specs if spec.key in chosen
        )
    )
    if {spec.key for spec in selected.specs} != chosen:
        raise ValueError("来源因子定义与筛选结果不一致")
    return baseline, selected


def _warehouse_data(provenance, mode, identifier):
    data = {"mode": mode, "warehouse_id": identifier}
    if "universe_filters" in provenance:
        data["filters"] = provenance["universe_filters"]
    if mode == "market_warehouse" and provenance.get("pool_codes") is not None:
        data["codes"] = provenance["pool_codes"]
    return data


def _source_data(evaluation, options):
    metadata = evaluation["metadata"]
    provenance = metadata.get("provenance", {})
    digest = provenance.get("manifest_sha256")
    for key, mode in (("historical_indices", "index_warehouse"), ("market_universes", "market_warehouse")):
        for option in options.get(key, []):
            if digest and option.get("manifest_sha256") == digest:
                return _warehouse_data(provenance, mode, option["id"]), option
    for option in options["snapshots"]:
        if option["id"] == metadata.get("data_version"):
            data = {
                "mode": "snapshot",
                "snapshot_id": option["id"],
                "codes": sorted(row["code"] for row in option["codes"]),
            }
            if "universe_filters" in provenance:
                data["filters"] = provenance["universe_filters"]
            return data, option
    raise ValueError("来源行情版本已不在可研究数据中，请恢复该版本或用现有数据重新评估后筛选")


def _source_backtest(store, evaluation, start, end):
    metadata = evaluation["metadata"]
    records, _ = store.list_metadata("backtests")
    matching = [row for row in records if row["metadata"].get("factor_run_id") == metadata.get("factor_run_id")]
    matching.sort(
        key=lambda row: (row.get("summary", {}).get("created_at", ""), row.get("summary", {}).get("id", "")),
        reverse=True,
    )
    values = {}
    if matching:
        saved = matching[0]["metadata"]
        provenance = saved.get("provenance", {})
        source = provenance.get("backtest", provenance)
        values = {
            key: source[key]
            for key in BacktestConfig.__dataclass_fields__
            if key in source and key not in {"start", "end"}
        }
        if "backtest" not in provenance:
            values.pop("benchmark", None)
        benchmark = saved.get("benchmark", {}).get("code")
        if benchmark and (benchmark != provenance.get("benchmark") or "backtest" in provenance):
            values["benchmark"] = benchmark
    return asdict(BacktestConfig(start, end, **values))


def _source_references(store, evaluation_id, data_root):
    evaluation = store.get("evaluations", evaluation_id, hydrate=False)
    metadata = evaluation["metadata"]
    factor = store.get("factors", metadata["factor_run_id"], hydrate=False)
    provenance = factor["metadata"].get("provenance", {})
    neutralization = dict(provenance.get("neutralization", {"mode": "off"}))
    if provenance.get("industry_version"):
        neutralization["industry_version"] = provenance["industry_version"]
    neutralization = parse_neutralization(neutralization).as_dict()
    financials = None
    version = metadata.get("provenance", {}).get("financial_version")
    if version:
        values = {"version": version}
        policy = metadata["provenance"].get("financial_availability_policy")
        if policy:
            values["availability_policy"] = policy
        financials = FinancialBinding.from_dict(financial_root(data_root), values)
    return {
        "evaluation": evaluation,
        "neutralization": neutralization,
        "financials": financials,
    }, provenance


def warehouse_source_context(store, evaluation_id, data_root, data, expected_sha):
    """Use only the stored source references and the manifest digest already checked by the worker."""
    context, _ = _source_references(store, evaluation_id, data_root)
    source = context["evaluation"]["metadata"].get("provenance", {})
    if source.get("manifest_sha256") != expected_sha:
        raise ValueError("筛选来源行情版本与所选仓库不一致，请恢复原版本或重新筛选")
    return {**context, "data": _warehouse_data(source, data["mode"], data["warehouse_id"])}


def source_context(store, evaluation_id, data_root, runs_root, *, frozen_snapshot=None, references_frozen=False):
    context, provenance = _source_references(store, evaluation_id, data_root)
    evaluation = context["evaluation"]
    metadata = evaluation["metadata"]
    options = research_options(
        data_root,
        runs_root,
        industry_version=provenance.get("industry_version"),
        financial_version=metadata.get("provenance", {}).get("financial_version"),
        allow_latest_references=not references_frozen,
        frozen_snapshot=frozen_snapshot,
    )
    data, option = _source_data(evaluation, options)
    return {
        **context,
        "data": data,
        "option": option,
        "financial_data": options["financial_data"],
        "industry_data": options["industry_data"],
        "backtest": _source_backtest(store, evaluation, metadata["start"], metadata["end"]),
    }
