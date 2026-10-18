@echo off
rem Local dev: run the worker against the WSL PostgreSQL (127.0.0.1:5433). Enable the LLM via env vars (see README).
rem ASCII only: cmd.exe reads this file in the system code page, not UTF-8.
setlocal
if "%SENSING_DB_DSN%"=="" set "SENSING_DB_DSN=postgresql://sensing:sensing@127.0.0.1:5433/sensing"
if "%SENSING_SECRET_KEY%"=="" set "SENSING_SECRET_KEY=local-dev-secret"
if "%SENSING_EVIDENCE_DIR%"=="" set "SENSING_EVIDENCE_DIR=%~dp0..\..\worker\data\evidence"
cd /d "%~dp0..\..\worker"
set "PYTHONPATH=src"
python -m sensing_worker.main %*
