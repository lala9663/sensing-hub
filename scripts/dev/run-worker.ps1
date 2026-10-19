# 로컬 개발: WSL PostgreSQL(127.0.0.1:5433)에 붙여 워커를 띄운다.
# LLM 키 등 로컬 전용 값은 scripts\dev\worker.local.env (git 제외, 예시: worker.local.env.example)에 둔다.
#   powershell -NoProfile -File scripts\dev\run-worker.ps1 [--once]
$ErrorActionPreference = "Stop"
$localEnv = Join-Path $PSScriptRoot "worker.local.env"
if (Test-Path $localEnv) {
    foreach ($line in Get-Content $localEnv -Encoding UTF8) {
        $t = $line.Trim()
        if (-not $t -or $t.StartsWith("#") -or -not $t.Contains("=")) { continue }
        $k, $v = $t.Split("=", 2)
        $k = $k.Trim(); $v = $v.Trim()
        if ($k -and $v -and -not [Environment]::GetEnvironmentVariable($k)) { [Environment]::SetEnvironmentVariable($k, $v) }
    }
}
if (-not $env:SENSING_DB_DSN) { $env:SENSING_DB_DSN = "postgresql://sensing:sensing@127.0.0.1:5433/sensing" }
if (-not $env:SENSING_SECRET_KEY) { $env:SENSING_SECRET_KEY = "local-dev-secret" }
if (-not $env:SENSING_EVIDENCE_DIR) { $env:SENSING_EVIDENCE_DIR = (Join-Path $PSScriptRoot "..\..\worker\data\evidence") }
Set-Location (Join-Path $PSScriptRoot "..\..\worker")
$env:PYTHONPATH = "src"
& python -m sensing_worker.main @args
exit $LASTEXITCODE
