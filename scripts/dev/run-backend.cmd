@echo off
rem Local dev: run the backend against the WSL PostgreSQL (127.0.0.1:5433, see scripts/dev/wsl-postgres.sh).
rem ASCII only: cmd.exe reads this file in the system code page, not UTF-8.
rem Use 127.0.0.1, not localhost (Java resolves localhost to ::1 first and misses the WSL port forward).
setlocal
if "%JAVA_HOME%"=="" set "JAVA_HOME=C:\Program Files\Java\jdk-23"
set "PATH=%JAVA_HOME%\bin;%PATH%"
if "%SENSING_DB_URL%"=="" set "SENSING_DB_URL=jdbc:postgresql://127.0.0.1:5433/sensing"
if "%SENSING_SECRET_KEY%"=="" set "SENSING_SECRET_KEY=local-dev-secret"
cd /d "%~dp0..\..\backend"
call gradlew.bat --no-daemon bootRun
