# 로컬 개발: WSL PostgreSQL(127.0.0.1:5433)에 붙여 워커를 띄운다. LLM은 환경 변수로 켠다(README 워커 환경 변수).
#   powershell -NoProfile -File scripts\dev\run-worker.ps1 [--once]
$ErrorActionPreference = "Stop"
if (-not $env:SENSING_DB_DSN) { $env:SENSING_DB_DSN = "postgresql://sensing:sensing@127.0.0.1:5433/sensing" }
if (-not $env:SENSING_SECRET_KEY) { $env:SENSING_SECRET_KEY = "local-dev-secret" }
if (-not $env:SENSING_EVIDENCE_DIR) { $env:SENSING_EVIDENCE_DIR = (Join-Path $PSScriptRoot "..\..\worker\data\evidence") }
Set-Location (Join-Path $PSScriptRoot "..\..\worker")
$env:PYTHONPATH = "src"
& python -m sensing_worker.main @args
exit $LASTEXITCODE
