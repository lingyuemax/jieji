# 一键启动（Windows PowerShell）
#   .\run.ps1            正常启动
#   .\run.ps1 -Install   先装依赖
#   .\run.ps1 -Check     只做自检，不启动

param(
    [switch]$Install,
    [switch]$Check
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$env:PYTHONIOENCODING = "utf-8"

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "找不到 python。装一个 3.10+，记得勾 'Add to PATH'。" -ForegroundColor Red
    exit 1
}

if ($Install) {
    Write-Host "安装依赖..." -ForegroundColor Cyan
    python -m pip install -r requirements.txt
}

if (-not (Test-Path ".env")) {
    if (Test-Path ".env.example") {
        Copy-Item ".env.example" ".env"
        Write-Host "已生成 .env，记得填 LLM_API_KEY（不填也能启动，只是不会决策）。" -ForegroundColor Yellow
    }
}

python scripts\selfcheck.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
if ($Check) { exit 0 }

Write-Host "`n启动中... 打开 http://127.0.0.1:8790" -ForegroundColor Green
python serve.py
