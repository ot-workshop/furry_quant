# Keep this file UTF-8 with BOM for Windows PowerShell 5.1.
param([switch]$Demo, [string]$Config, [ValidateRange(1, 65535)][int]$Port = 8000, [switch]$RunResearch)
$ErrorActionPreference = 'Stop'
$taskConsoleEncoding = [Console]::OutputEncoding
Push-Location $PSScriptRoot
try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $OutputEncoding = [Console]::OutputEncoding
    $taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $taskPython)) {
        throw '请先按 README 执行 uv sync --extra dev。'
    }
    if (-not (Test-Path -LiteralPath 'frontend\dist\index.html')) {
        throw '请先按 README 安装前端依赖并执行 npm.cmd --prefix frontend run build。'
    }
    if ($Demo -and $Config) { throw 'Demo and Config cannot be used together.' }
    if ($Demo -and $RunResearch) { throw 'Demo and RunResearch cannot be used together.' }
    if (-not $Demo -and -not $Config -and (Test-Path -LiteralPath 'runs\active-research.json')) {
        $Config = 'runs\active-research.json'
    }
    $taskListener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
    try {
        $taskListener.Start()
    } catch {
        throw "无法监听本机端口 $Port。若旧服务仍在运行，请关闭原窗口，或使用 .\start.ps1 -Port 8001。"
    } finally {
        $taskListener.Stop()
    }

    # Opening the workbench must not wait for research computations.
    if ($Demo) {
        Write-Host '[准备样本] 正在生成合成样本回测，请等待完成。'
        & $taskPython -u -X utf8 -m finance_quant.cli demo
        if ($LASTEXITCODE -ne 0) { throw '样本回测未成功，请检查上方错误。' }
    } elseif ($RunResearch) {
        Write-Host '[计算研究] 正在运行回测；完成后计算因子评估并启动网页。'
        if ($Config) {
            & $taskPython -u -X utf8 -m finance_quant.cli real --config $Config
        } elseif (Test-Path -LiteralPath 'data\tushare\20260803-20260930\manifest.json') {
            & $taskPython -u -X utf8 -m finance_quant.cli real
        } else {
            throw '请用 -Config 指定研究配置；也可直接启动网页后在“新建研究”提交任务。'
        }
        if ($LASTEXITCODE -ne 0) { throw '回测未成功，请检查上方错误。' }
        if ($Config) {
            Write-Host '[因子评估] 正在计算 IC、分组收益及报告。'
            & $taskPython -u -X utf8 -m finance_quant.cli evaluate --config $Config
            if ($LASTEXITCODE -ne 0) { throw '因子评估未成功，请检查上方错误。' }
        }
    }

    Write-Host "[启动网页] http://127.0.0.1:$Port （等待下方 Application startup complete；按 Ctrl+C 停止）"
    Write-Host '默认只启动服务；请在网页“新建研究”中运行计算。'
    if ($Config) {
        & $taskPython -u -X utf8 -m finance_quant.cli serve --config $Config --port $Port
    } elseif (-not $Demo -and (Test-Path -LiteralPath 'data\managed-joint\manifest.json')) {
        & $taskPython -u -X utf8 -m finance_quant.cli serve --data-dir 'data\managed-joint' --port $Port
    } elseif (-not $Demo -and (Test-Path -LiteralPath 'data\tushare\20260803-20260930\manifest.json')) {
        & $taskPython -u -X utf8 -m finance_quant.cli serve --data-dir 'data\tushare\20260803-20260930' --port $Port
    } else {
        & $taskPython -u -X utf8 -m finance_quant.cli serve --port $Port
    }
    if ($LASTEXITCODE -ne 0) { throw '网页服务启动失败，请检查上方错误。' }
} finally {
    [Console]::OutputEncoding = $taskConsoleEncoding
    Pop-Location
}
