@echo off
rem 로컬 개발: WSL PostgreSQL(127.0.0.1:5433, scripts/dev/wsl-postgres.sh)에 붙여 백엔드를 띄운다.
rem localhost 대신 127.0.0.1을 쓴다 — Java가 localhost를 IPv6(::1)로 먼저 풀면 WSL 포트 포워딩에 못 붙는다.
setlocal
if "%JAVA_HOME%"=="" set "JAVA_HOME=C:\Program Files\Java\jdk-23"
set "PATH=%JAVA_HOME%\bin;%PATH%"
if "%SENSING_DB_URL%"=="" set "SENSING_DB_URL=jdbc:postgresql://127.0.0.1:5433/sensing"
if "%SENSING_SECRET_KEY%"=="" set "SENSING_SECRET_KEY=local-dev-secret"
cd /d "%~dp0..\..\backend"
call gradlew.bat --no-daemon bootRun
