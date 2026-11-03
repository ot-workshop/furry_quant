import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from finance_quant import runtime


def test_windows_312_runtime_uses_native_fallback_and_keeps_user_thread_limit(monkeypatch):
    original = lambda *args: pytest.fail('WMI must not be called')
    platform = SimpleNamespace(_wmi_query=original)
    monkeypatch.setattr(runtime, 'platform', platform)
    monkeypatch.setattr(runtime, 'sys', SimpleNamespace(platform='win32', version_info=(3, 12, 10)))
    monkeypatch.setenv('OPENBLAS_NUM_THREADS', '2')
    monkeypatch.delenv('OMP_NUM_THREADS', raising=False)
    runtime.configure_runtime()
    runtime.configure_runtime()
    with pytest.raises(OSError):
        platform._wmi_query('CPU', 'Architecture')
    assert os.environ['OPENBLAS_NUM_THREADS'] == '2'
    assert os.environ['OMP_NUM_THREADS'] == '1'


@pytest.mark.parametrize('system,version', [('linux',(3,12)),('win32',(3,11)),('win32',(3,13))])
def test_other_platforms_do_not_replace_platform_queries(monkeypatch, system, version):
    original = lambda *args: ['native']
    platform = SimpleNamespace(_wmi_query=original)
    monkeypatch.setattr(runtime, 'platform', platform)
    monkeypatch.setattr(runtime, 'sys', SimpleNamespace(platform=system, version_info=version))
    runtime.configure_runtime()
    assert platform._wmi_query is original


def child_environment():
    environment = os.environ.copy()
    # Check the production entry points without the previous QA workaround.
    environment.pop('FINANCE_QUANT_QA_PLATFORM_FALLBACK', None)
    environment.pop('PYTHONPATH', None)
    return environment


def test_serve_loads_config_and_app_without_importing_research_engine(tmp_path):
    config = tmp_path / 'research.json'
    config.write_text(json.dumps({
        'data': {'root':'data', 'start':'20250101', 'end':'20260930', 'codes':['600000.SH','000001.SZ']},
        'backtest': {'start':'2025-09-01', 'end':'2026-09-30', 'topk':1}, 'runs_dir':'results',
    }), encoding='utf-8')
    script = '''
import sys, types
class NoEngine:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith('qlib') or fullname in {'finance_quant.application.real_workflow', 'finance_quant.application.evaluation_workflow'}:
            raise AssertionError('Web startup must not load the calculation engine: ' + fullname)
sys.meta_path.insert(0, NoEngine())
uvicorn = types.ModuleType('uvicorn')
def run(app, **kwargs):
    assert kwargs == {'host':'127.0.0.1', 'port':8123}
    assert app.state.research_tasks.runs_root.name == 'results'
    assert app.state.research_tasks.data_root.name == 'data'
uvicorn.run = run
sys.modules['uvicorn'] = uvicorn
from finance_quant.cli import main
sys.argv = ['finance-quant', 'serve', '--config', sys.argv[1], '--port', '8123']
main()
print('SERVE_ONLY_OK')
'''
    result = subprocess.run([sys.executable, '-X', 'utf8', '-c', script, str(config)],
                            capture_output=True, text=True, encoding='utf-8', env=child_environment(), timeout=20)
    assert result.returncode == 0, result.stderr
    assert 'SERVE_ONLY_OK' in result.stdout
    assert not (tmp_path / 'results').exists()


@pytest.mark.skipif(sys.platform != 'win32' or sys.version_info[:2] != (3,12), reason='Windows Python 3.12 regression')
def test_worker_bootstrap_prevents_wmi_even_when_loading_native_qlib():
    script = '''
import platform
def forbidden(*args, **kwargs):
    raise AssertionError('Scientific import attempted WMI')
platform._wmi_query = forbidden
import finance_quant.application.task_worker
from qlib.backtest import backtest
from qlib.contrib.strategy.signal_strategy import TopkDropoutStrategy
assert isinstance(platform.machine(), str)
print('WORKER_BOOTSTRAP_OK')
'''
    result = subprocess.run([sys.executable, '-X', 'utf8', '-c', script], capture_output=True,
                            text=True, encoding='utf-8', env=child_environment(), timeout=20)
    assert result.returncode == 0, result.stderr
    assert 'WORKER_BOOTSTRAP_OK' in result.stdout


@pytest.mark.skipif(shutil.which('powershell.exe') is None, reason='Windows PowerShell 5.1 parser')
def test_start_script_keeps_bom_and_parses_in_windows_powershell():
    path = Path(__file__).parents[1] / 'start.ps1'
    assert path.read_bytes().startswith(b'\xef\xbb\xbf')
    command = "& { param($path) $tokens=$null; $errors=$null; [System.Management.Automation.Language.Parser]::ParseFile($path,[ref]$tokens,[ref]$errors) > $null; if ($errors.Count) { $errors | Out-String | Write-Error; exit 1 } }"
    result = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',command,str(path)],
                            capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
