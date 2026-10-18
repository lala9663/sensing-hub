@echo off
rem 로컬 개발: WSL PostgreSQL(127.0.0.1:5433)에 붙여 워커를 띄운다. LLM은 환경 변수로 켠다(README 워커 환경 변수).
setlocal
if "%SENSING_DB_DSN%"=="" set "SENSING_DB_DSN=postgresql://sensing:sensing@127.0.0.1:5433/sensing"
if "%SENSING_SECRET_KEY%"=="" set "SENSING_SECRET_KEY=local-dev-secret"
if "%SENSING_EVIDENCE_DIR%"=="" set "SENSING_EVIDENCE_DIR=%~dp0..\..\worker\data\evidence"
cd /d "%~dp0..\..\worker"
set "PYTHONPATH=src"
python -m sensing_worker.main %*
