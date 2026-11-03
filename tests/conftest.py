import os

for variable in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ[variable] = "1"

os.environ["MLFLOW_DISABLE_AGENT_HINT"] = "1"

import pytest
@pytest.fixture(autouse=True)
def isolated_network_accounting(tmp_path,monkeypatch):
    # Mock API calls and isolated test workers must not consume the live ledger.
    monkeypatch.setenv('FINANCE_QUANT_QUOTA_DB',str(tmp_path/'network-budget.sqlite3'))
    monkeypatch.setenv('FINANCE_QUANT_INDUSTRY_ROOT',str(tmp_path/'industries'))
