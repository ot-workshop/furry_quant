"""Regression checks for independently usable task contracts and extracted stages."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from test_factor_library import frame
from test_industry_neutralization import history

import finance_quant
from finance_quant.research.market_factors import market_factors
from finance_quant.research.neutralization import NeutralizationConfig
from finance_quant.research.registry import FactorConfig, FactorSpec


def test_task_storage_and_context_import_without_the_worker_or_submission_service(tmp_path):
    script = """
import sys
from finance_quant.persistence.tasks import PgTaskStore, conflict
from finance_quant.application.task_context import TaskContext
from finance_quant.task_contracts import TaskConflict
assert isinstance(conflict('conflict'), TaskConflict)
assert 'finance_quant.application.task_worker' not in sys.modules
assert 'finance_quant.application.tasks' not in sys.modules
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_secondary_validation_changes_invalidate_asset_cache(tmp_path):

    package = Path(finance_quant.__file__).parent
    shutil.copytree(package, tmp_path / "finance_quant", ignore=shutil.ignore_patterns("__pycache__"))
    script = """
from pathlib import Path
from finance_quant.application.quote_capture import validation_signature
from finance_quant.application import quote_quality
before = validation_signature()
path = Path(quote_quality.__file__)
path.write_text(path.read_text(encoding='utf-8') + '\\n# changed validation implementation\\n', encoding='utf-8')
assert validation_signature() != before
"""
    environment = {**os.environ, "PYTHONPATH": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr


def test_streaming_keeps_exclusions_when_early_industry_sections_have_no_variation():
    market = frame(8, 40)
    dates = sorted(market.date.unique())
    market.loc[market.date.isin(dates[:15]), "pe_ttm"] = 10.0
    industry = history(sorted(market.instrument.unique()))
    config = FactorConfig((FactorSpec("value", 1.0),))
    kwargs = {"industry_history": industry, "neutralization": NeutralizationConfig("industry")}
    expected = market_factors(market, "fixture", config, **kwargs)
    tables = {"history": [], "diagnostics": []}
    batches = []

    def sink(name, rows):
        tables[name].extend(rows)
        batches.append((name, len(rows)))

    actual = market_factors(market, "fixture", config, row_sink=sink, **kwargs)
    assert actual["history"] == actual["diagnostics"] == []
    assert actual["metadata"] == expected["metadata"]
    assert tables["history"] == expected["history"]
    assert tables["diagnostics"] == expected["diagnostics"]
    assert batches[:2] == [("diagnostics", 0), ("diagnostics", 8)]
    assert any("no_within_industry_variation" in row["reasons"].values() for row in tables["diagnostics"])
