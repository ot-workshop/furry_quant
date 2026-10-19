# Keep this file UTF-8 with BOM for Windows PowerShell 5.1.
param([switch]$Demo, [string]$Config, [ValidateRange(1, 65535)][int]$Port = 8000)
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    $taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $taskPython)) {
        throw '请先按 README 执行 uv sync --extra dev。'
    }
    if (-not (Test-Path -LiteralPath 'frontend\dist\index.html')) {
        throw '请先按 README 安装前端依赖并执行 npm.cmd --prefix frontend run build。'
    }
    if ($Demo -and $Config) { throw 'Demo and Config cannot be used together.' }
    if (-not $Demo -and -not $Config -and (Test-Path -LiteralPath 'runs\active-research.json')) {
        $Config = 'runs\active-research.json'
    }
    if ($Config) {
        & $taskPython -m finance_quant.cli real --config $Config
    } elseif (-not $Demo -and (Test-Path -LiteralPath 'data\tushare\20260803-20260930\manifest.json')) {
        & $taskPython -m finance_quant.cli real
    } else {
        & $taskPython -m finance_quant.cli demo
    }
    if ($LASTEXITCODE -ne 0) { throw '回测未成功，请检查上方错误。' }
    if ($Config) {
        & $taskPython -m finance_quant.cli evaluate --config $Config
        if ($LASTEXITCODE -ne 0) { throw '因子评估未成功，请检查上方错误。' }
    }
    if ($Config) {
        & $taskPython -m finance_quant.cli serve --config $Config --port $Port
    } else {
        & $taskPython -m finance_quant.cli serve --port $Port
    }
} finally {
    Pop-Location
}
