import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from finance_quant import runtime


def test_windows_312_runtime_uses_native_fallback_and_keeps_user_thread_limit(monkeypatch):
    original = lambda *args: pytest.fail("WMI must not be called")
    platform = SimpleNamespace(_wmi_query=original)
    monkeypatch.setattr(runtime, "platform", platform)
    monkeypatch.setattr(runtime, "sys", SimpleNamespace(platform="win32", version_info=(3, 12, 10)))
    monkeypatch.setenv("OPENBLAS_NUM_THREADS", "2")
    monkeypatch.delenv("OMP_NUM_THREADS", raising=False)
    runtime.configure_runtime()
    runtime.configure_runtime()
    with pytest.raises(OSError):
        platform._wmi_query("CPU", "Architecture")
    assert os.environ["OPENBLAS_NUM_THREADS"] == "2"
    assert os.environ["OMP_NUM_THREADS"] == "1"


@pytest.mark.parametrize("system,version", [("linux", (3, 12)), ("win32", (3, 11)), ("win32", (3, 13))])
def test_other_platforms_do_not_replace_platform_queries(monkeypatch, system, version):
    original = lambda *args: ["native"]
    platform = SimpleNamespace(_wmi_query=original)
    monkeypatch.setattr(runtime, "platform", platform)
    monkeypatch.setattr(runtime, "sys", SimpleNamespace(platform=system, version_info=version))
    runtime.configure_runtime()
    assert platform._wmi_query is original


def child_environment():
    environment = os.environ.copy()
    # Check the production entry points without the previous QA workaround.
    environment.pop("FINANCE_QUANT_QA_PLATFORM_FALLBACK", None)
    environment.pop("PYTHONPATH", None)
    return environment


@pytest.mark.parametrize(
    "modules",
    [
        ("finance_quant.application.task_worker", "finance_quant.server"),
        ("finance_quant.server", "finance_quant.application.task_worker"),
        ("finance_quant.research.factor_library", "finance_quant.research.fundamentals"),
        ("finance_quant.research.fundamentals", "finance_quant.research.factor_library"),
        ("finance_quant.data.providers.baostock_worker", "finance_quant.research_api"),
    ],
)
def test_module_imports_do_not_connect_or_load_settings(tmp_path, modules):
    """Eager imports must be safe in API, worker and research entry orders."""
    script = """
import importlib
import socket
import sys
from pathlib import Path
import dotenv
import sqlalchemy

def forbidden(*args, **kwargs):
    raise AssertionError('Import attempted external work')

socket.socket.connect = forbidden
socket.create_connection = forbidden
sqlalchemy.create_engine = forbidden
dotenv.load_dotenv = forbidden
dotenv.dotenv_values = forbidden
for module in sys.argv[2:]:
    importlib.import_module(module)
assert not Path(sys.argv[1]).exists()
print('IMPORT_OK')
"""
    root = tmp_path / "storage-must-not-exist"
    environment = child_environment()
    environment.update(
        STORAGE_ROOT=str(root),
        FINANCE_QUANT_DATA=str(root / "data"),
        FINANCE_QUANT_RUNS=str(root / "runs"),
        FINANCE_QUANT_QUOTA_DB=str(root / "quota.sqlite3"),
        FINANCE_QUANT_INDUSTRY_ROOT=str(root / "industries"),
        FINANCE_QUANT_BENCHMARK_ROOT=str(root / "benchmarks"),
    )
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", script, str(root), *modules],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        cwd=Path(__file__).resolve().parents[1],
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "IMPORT_OK" in result.stdout


@pytest.mark.skipif(sys.platform != "win32" or sys.version_info[:2] != (3, 12), reason="Windows Python 3.12 regression")
@pytest.mark.parametrize("entrypoint", ["finance_quant.application.task_worker", "finance_quant.server"])
def test_bootstrap_prevents_wmi_even_when_loading_native_qlib(entrypoint):
    script = """
import importlib
import platform
import sys
def forbidden(*args, **kwargs):
    raise AssertionError('Scientific import attempted WMI')
platform._wmi_query = forbidden
importlib.import_module(sys.argv[1])
from qlib.backtest import backtest
from qlib.contrib.strategy.signal_strategy import TopkDropoutStrategy
assert isinstance(platform.machine(), str)
print('WORKER_BOOTSTRAP_OK')
"""
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", script, entrypoint],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=child_environment(),
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "WORKER_BOOTSTRAP_OK" in result.stdout
