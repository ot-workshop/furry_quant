import os
import subprocess
import sys
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


@pytest.mark.skipif(sys.platform != "win32" or sys.version_info[:2] != (3, 12), reason="Windows Python 3.12 regression")
def test_worker_bootstrap_prevents_wmi_even_when_loading_native_qlib():
    script = """
import platform
def forbidden(*args, **kwargs):
    raise AssertionError('Scientific import attempted WMI')
platform._wmi_query = forbidden
import finance_quant.application.task_worker
from qlib.backtest import backtest
from qlib.contrib.strategy.signal_strategy import TopkDropoutStrategy
assert isinstance(platform.machine(), str)
print('WORKER_BOOTSTRAP_OK')
"""
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=child_environment(),
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "WORKER_BOOTSTRAP_OK" in result.stdout
