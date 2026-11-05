# AGY-BabelDoc One-Click Environment Bootstrapper (Windows)
$ErrorActionPreference = "Stop"

Write-Host "=== Initializing AGY-BabelDoc v2.0 Environment ===" -ForegroundColor Cyan

if (-not (Get-Command "uv" -ErrorAction SilentlyContinue)) {
    Write-Host "[1/4] Installing uv package manager..." -ForegroundColor Yellow
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
} else {
    Write-Host "[1/4] uv is already installed." -ForegroundColor Green
}

$mcpPy = "$env:APPDATA\uv\tools\pdf2zh-next-mcp\Scripts\python.exe"
if (-not (Test-Path $mcpPy)) {
    Write-Host "[2/4] Installing pdf2zh-next-mcp tool environment..." -ForegroundColor Yellow
    uv tool install pdf2zh-next-mcp
} else {
    Write-Host "[2/4] pdf2zh-next-mcp environment ready." -ForegroundColor Green
}

$babelPy = "$env:APPDATA\uv\tools\pdf2zh-next\Scripts\python.exe"
if (-not (Test-Path $babelPy)) {
    Write-Host "[3/4] Installing pdf2zh-next (BabelDOC) tool environment..." -ForegroundColor Yellow
    uv tool install pdf2zh-next
} else {
    Write-Host "[3/4] pdf2zh-next environment ready." -ForegroundColor Green
}

Write-Host "[4/4] Configuring DirectML GPU acceleration (onnxruntime-directml==1.20.1)..." -ForegroundColor Yellow
uv pip uninstall --python $babelPy onnxruntime onnxruntime-directml 2>$null
uv pip install --python $babelPy "onnxruntime-directml==1.20.1"
Set-Content -Path "$env:APPDATA\uv\tools\pdf2zh-next\.dml_ready" -Value "ok" -Encoding UTF8

Write-Host "=== AGY-BabelDoc v2.0 is ready! Restart or toggle the plugin in Antigravity to use. ===" -ForegroundColor Green
