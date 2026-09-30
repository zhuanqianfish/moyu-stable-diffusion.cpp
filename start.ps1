<#
    stable-diffusion.cpp Web 生图服务 —— PowerShell 启动脚本

    用法：
        powershell -ExecutionPolicy Bypass -File .\start.ps1
#>

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

Write-Host ""
Write-Host "==============================================================" -ForegroundColor DarkCyan
Write-Host "   stable-diffusion.cpp  Web 生图服务" -ForegroundColor Cyan
Write-Host "--------------------------------------------------------------" -ForegroundColor DarkCyan
Write-Host "   项目目录 : $Root"
Write-Host "==============================================================" -ForegroundColor DarkCyan
Write-Host ""

# ---------- 1. 检查引擎 ----------
$serverExe = Join-Path $Root "bin\sd-server.exe"
if (-not (Test-Path $serverExe)) {
    Write-Host "[错误] 未找到推理引擎: $serverExe" -ForegroundColor Red
    Write-Host "       请先执行部署脚本: python .\scripts\setup.py"
    exit 1
}

# ---------- 2. 模型路径来自 model_path.json（不再检查本地 models\ 目录） ----------
$modelPathFile = Join-Path $Root "model_path.json"
if (Test-Path $modelPathFile) {
    Write-Host "[信息] 模型路径配置: model_path.json" -ForegroundColor Green
} else {
    Write-Host "[提示] 未找到 model_path.json，将回退到 webui\config.json 的 paths" -ForegroundColor Yellow
}
Write-Host ""

# ---------- 3. 查找 Python ----------
$pyCmd = $null
$pyArgs = @()

function Test-Python($exe, $pre) {
    try {
        $v = & $exe @pre "-c" "import sys; print(sys.version_info[0])" 2>$null
        return ($v -eq "3")
    } catch { return $false }
}

if (Get-Command py -ErrorAction SilentlyContinue) {
    if (Test-Python "py" @("-3")) { $pyCmd = "py"; $pyArgs = @("-3") }
}
if (-not $pyCmd -and (Get-Command python -ErrorAction SilentlyContinue)) {
    if (Test-Python "python" @()) { $pyCmd = "python"; $pyArgs = @() }
}
if (-not $pyCmd) {
    $candidates = @(
        (Join-Path $env:USERPROFILE ".workbuddy-ai\binaries\python\versions\3.13.12\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe")
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { $pyCmd = $c; $pyArgs = @(); break }
    }
}

if (-not $pyCmd) {
    Write-Host "[错误] 未检测到可用的 Python 3。" -ForegroundColor Red
    Write-Host "       请安装 Python 3.10+ : https://www.python.org/downloads/"
    exit 1
}

Write-Host "[信息] 使用 Python: $pyCmd" -ForegroundColor Green
Write-Host "[信息] 正在启动服务，浏览器会自动打开。" -ForegroundColor Green
Write-Host "[信息] 按 Ctrl+C 可停止服务。" -ForegroundColor DarkGray
Write-Host ""

$serverPy = Join-Path $Root "webui\server.py"
& $pyCmd @pyArgs $serverPy
$rc = $LASTEXITCODE
if ($null -eq $rc) { $rc = 0 }

Write-Host ""
if ($rc -ne 0) {
    Write-Host "[错误] 服务异常退出，返回码 $rc" -ForegroundColor Red
    Write-Host "       请查看日志: $Root\logs\sd-server.log"
} else {
    Write-Host "[信息] 服务已停止。" -ForegroundColor Green
}
exit $rc
