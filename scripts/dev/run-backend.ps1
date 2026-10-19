# 로컬 개발: WSL PostgreSQL(127.0.0.1:5433, scripts/dev/wsl-postgres.sh)에 붙여 백엔드를 띄운다.
# localhost 대신 127.0.0.1을 쓴다 — Java가 localhost를 IPv6(::1)로 먼저 풀면 WSL 포트 포워딩에 못 붙는다.
# 챗봇 LLM·의미 검색 임베딩 설정은 워커와 한 곳에서 관리한다: scripts\dev\worker.local.env(git 제외)의 SENSING_LLM_*·SENSING_EMBED_*·ANTHROPIC_* 값을 읽는다
# (이미 설정된 환경 변수는 덮어쓰지 않는다. 키 값은 찍지 않는다).
#   powershell -NoProfile -File scripts\dev\run-backend.ps1
$ErrorActionPreference = "Stop"
$localEnv = Join-Path $PSScriptRoot "worker.local.env"
if (Test-Path $localEnv) {
    foreach ($line in Get-Content $localEnv -Encoding UTF8) {
        $t = $line.Trim()
        if (-not $t -or $t.StartsWith("#") -or -not $t.Contains("=")) { continue }
        $k, $v = $t.Split("=", 2)
        $k = $k.Trim(); $v = $v.Trim()
        if (-not ($k -like "SENSING_LLM_*" -or $k -like "SENSING_EMBED_*" -or $k -like "ANTHROPIC_*")) { continue }
        if ($k -and $v -and -not [Environment]::GetEnvironmentVariable($k)) { [Environment]::SetEnvironmentVariable($k, $v) }
    }
}
$provider = if ($env:SENSING_LLM_PROVIDER) { $env:SENSING_LLM_PROVIDER } else { "none" }
$hasKey = [bool]($env:SENSING_LLM_API_KEY -or $env:ANTHROPIC_API_KEY)
Write-Host "chat LLM: provider=$provider model=$($env:SENSING_LLM_MODEL) key=$(if ($hasKey) { 'set' } else { 'none' })"
$embed = if ($env:SENSING_EMBED_PROVIDER) { $env:SENSING_EMBED_PROVIDER } else { "none" }
Write-Host "search embedding: provider=$embed model=$($env:SENSING_EMBED_MODEL)"
if (-not $env:JAVA_HOME) { $env:JAVA_HOME = "C:\Program Files\Java\jdk-23" }
$env:Path = "$env:JAVA_HOME\bin;$env:Path"
if (-not $env:SENSING_DB_URL) { $env:SENSING_DB_URL = "jdbc:postgresql://127.0.0.1:5433/sensing" }
if (-not $env:SENSING_SECRET_KEY) { $env:SENSING_SECRET_KEY = "local-dev-secret" }
Set-Location (Join-Path $PSScriptRoot "..\..\backend")
& .\gradlew.bat --no-daemon bootRun
exit $LASTEXITCODE
