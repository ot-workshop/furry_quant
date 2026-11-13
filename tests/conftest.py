import os
from dataclasses import asdict

for variable in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ[variable] = "1"

os.environ["MLFLOW_DISABLE_AGENT_HINT"] = "1"

import pytest
from test_managed_data import Provider

from finance_quant.configuration import BacktestConfig
from finance_quant.data import shared_market
from finance_quant.research.registry import EvaluationConfig, FactorConfig
from support.file_market import FileMarketStore
from support.fixed_pool import publish_fixture_snapshot


@pytest.fixture(autouse=True)
def isolated_network_accounting(tmp_path, monkeypatch):
    # Mock API calls and isolated test workers must not consume the live ledger.
    monkeypatch.setenv("FINANCE_QUANT_QUOTA_DB", str(tmp_path / "network-budget.sqlite3"))
    monkeypatch.setenv("FINANCE_QUANT_INDUSTRY_ROOT", str(tmp_path / "industries"))


@pytest.fixture(autouse=True)
def market_store():
    """Production configures PostgresMarketStore; unit tests use the file-backed double."""
    store = FileMarketStore()
    shared_market.configure_store(store)
    yield store
    shared_market.configure_store(None)


@pytest.fixture
def workspace(tmp_path):
    """A published four-stock snapshot and a valid three-stock research request."""

    data_root, runs_root = tmp_path / "data", tmp_path / "runs"
    codes = ("600000.SH", "000001.SZ", "600519.SH", "600036.SH")
    manifest = publish_fixture_snapshot(data_root, codes, "20241202", "20250228", provider=Provider())
    request = {
        "name": "网页研究",
        "data": {"mode": "snapshot", "snapshot_id": manifest["id"], "codes": list(codes[:3])},
        "backtest": asdict(BacktestConfig("2025-01-02", "2025-02-28", topk=2)),
        "factors": FactorConfig().as_list(),
        "evaluation": EvaluationConfig(horizons=(1, 3)).as_dict(),
    }
    return data_root, runs_root, request
