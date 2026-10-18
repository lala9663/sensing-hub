# 로컬 개발: WSL PostgreSQL(127.0.0.1:5433, scripts/dev/wsl-postgres.sh)에 붙여 백엔드를 띄운다.
# localhost 대신 127.0.0.1을 쓴다 — Java가 localhost를 IPv6(::1)로 먼저 풀면 WSL 포트 포워딩에 못 붙는다.
#   powershell -NoProfile -File scripts\dev\run-backend.ps1
$ErrorActionPreference = "Stop"
if (-not $env:JAVA_HOME) { $env:JAVA_HOME = "C:\Program Files\Java\jdk-23" }
$env:Path = "$env:JAVA_HOME\bin;$env:Path"
if (-not $env:SENSING_DB_URL) { $env:SENSING_DB_URL = "jdbc:postgresql://127.0.0.1:5433/sensing" }
if (-not $env:SENSING_SECRET_KEY) { $env:SENSING_SECRET_KEY = "local-dev-secret" }
Set-Location (Join-Path $PSScriptRoot "..\..\backend")
& .\gradlew.bat --no-daemon bootRun
exit $LASTEXITCODE
