"""Versioned factor artifacts shared by execution and research diagnostics."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock, Timeout

from finance_quant.data.financials import FinancialBinding
from finance_quant.data.market import load_market
from finance_quant.provenance import fingerprint, source_provenance
from finance_quant.research.fundamentals import financial_provenance, fundamentals_for
from finance_quant.research.market_factors import market_factors
from finance_quant.research.neutralization import NeutralizationConfig, industry_context
from finance_quant.research.registry import FactorConfig
from finance_quant.storage import ResultStore

__all__ = ["fingerprint", "load_configured_snapshot", "publish_factor_run", "source_provenance"]


def load_configured_snapshot(config):
    snapshot = load_market(config.data.root)
    manifest = snapshot[3]
    if (
        set(manifest["codes"]) != set(config.data.codes)
        or manifest["start"] != config.data.start
        or manifest["end"] != config.data.end
        or manifest.get("benchmark", "000300.SH") != config.data.benchmark
        or manifest.get("cross_validation", {}).get("mode", "off") != config.data.validation.mode
    ):
        raise ValueError(
            "Published snapshot differs from configured data range, stock pool or benchmark; run download with this config first"
        )
    return snapshot


def publish_factor_run(
    market,
    manifest: dict,
    runs_dir: Path,
    config: FactorConfig | None = None,
    neutralization=None,
    *,
    result_store=None,
    financials: FinancialBinding | None = None,
) -> dict:
    config = config or FactorConfig()

    neutralization = neutralization or NeutralizationConfig()
    industry = industry_context(neutralization)
    provenance = {
        **source_provenance(),
        "data_version": manifest["id"],
        "factors": config.as_list(),
        "neutralization": neutralization.as_dict(),
        **financial_provenance(financials, config),
    }
    if industry:
        provenance["industry_version"] = industry.manifest["id"]
    run_id = "real-factors-" + fingerprint(provenance)
    store = result_store or ResultStore(runs_dir)
    locks = runs_dir / ".locks"
    locks.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(locks / (run_id + ".lock"), timeout=0):
            if store.exists("factors", run_id):
                existing = store.get("factors", run_id)
                if existing["metadata"]["provenance"] != provenance:
                    raise ValueError("Factor artifact provenance conflict")
                return existing
            result = market_factors(
                market,
                manifest["id"],
                config,
                neutralization=neutralization,
                industry_history=industry,
                financials=fundamentals_for(financials, config, market),
            )
            result["metadata"].update(
                id=run_id, provenance=provenance, created_at=datetime.now(timezone.utc).isoformat()
            )
            result["metadata"]["source"] = manifest.get("source", "third_party_tushare_compatible")
            if manifest.get("cross_validation"):
                result["metadata"]["cross_validation"] = {
                    "policy": manifest["cross_validation"]["policy"],
                    "status": manifest["cross_validation"]["status"],
                    "stocks": [
                        {k: row[k] for k in ["code", "status", "price_source", "adjustment_source"]}
                        for row in manifest["cross_validation"]["stocks"]
                    ],
                }
                result["metadata"]["notes"] += [
                    "联合数据：回退股票的整段行情与复权来自BaoStock；估值仍取自Tushare，详见数据管理中的来源报告"
                ]
            store.publish("factors", run_id, result)
            return result
    except Timeout:
        raise ValueError("This factor configuration is already running") from None
