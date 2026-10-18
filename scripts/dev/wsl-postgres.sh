#!/usr/bin/env bash
# 로컬 개발용 PostgreSQL 16 + pgvector를 WSL Ubuntu 안에 설치한다 (Docker·Windows 관리자 권한 없이).
#
#   wsl -d Ubuntu -- sudo bash /mnt/c/<이 저장소 경로>/scripts/dev/wsl-postgres.sh
#
# - 포트 5433 (Windows에 설치된 PostgreSQL이 5432를 쓰는 경우가 있어 피한다)
# - 계정 sensing/sensing (개발용 superuser: Flyway가 CREATE EXTENSION vector/pgcrypto를 실행한다)
# - 데이터베이스 sensing(앱), sensing_test(워커 테스트), sensing_it(백엔드 통합 테스트)
# - Windows 쪽에서는 127.0.0.1:5433 으로 붙는다 (WSL2 localhost 포워딩). "localhost"는 Java·Python이 IPv6(::1)로 먼저 풀어
#   연결이 거부되거나 멈추므로 반드시 127.0.0.1을 쓴다. WSL NAT 안에서만 열린다.
# - WSL은 마지막 프로세스가 끝나면 몇 초 뒤 꺼지고 PostgreSQL도 함께 죽는다. systemd가 켜진 배포판이면 `systemctl enable postgresql`로
#   WSL이 뜰 때 같이 뜨고, 세션 동안 살려 두려면 `wsl -d Ubuntu -u root -- tail -f /dev/null` 같은 프로세스를 하나 띄워 둔다.
#   sudo 비밀번호 없이 돌리려면 `wsl -d Ubuntu -u root -- bash <이 스크립트>` 로 실행한다.
#
# 쓰는 쪽 설정 (scripts/dev/run-backend.ps1, run-worker.ps1가 이 값을 기본으로 쓴다):
#   backend: SENSING_DB_URL=jdbc:postgresql://127.0.0.1:5433/sensing  (테스트: SENSING_IT_DB_URL=jdbc:postgresql://127.0.0.1:5433/sensing_it)
#   worker : SENSING_DB_DSN=postgresql://sensing:sensing@127.0.0.1:5433/sensing (테스트: SENSING_TEST_DSN=...@127.0.0.1:5433/sensing_test)
set -euo pipefail

PORT="${PORT:-5433}"
PG=16

export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q postgresql-${PG} postgresql-${PG}-pgvector

CONF=/etc/postgresql/${PG}/main/postgresql.conf
HBA=/etc/postgresql/${PG}/main/pg_hba.conf
sed -i -E "s/^#?port = .*/port = ${PORT}/" "$CONF"
sed -i -E "s/^#?listen_addresses = .*/listen_addresses = '*'/" "$CONF"
grep -q "sensing-hub dev" "$HBA" || cat >> "$HBA" <<'EOF'

# sensing-hub dev: Windows(WSL2 NAT)에서 들어오는 접속. 비밀번호 인증
host    all             all             0.0.0.0/0               scram-sha-256
EOF

service postgresql restart

su postgres -c "psql -p ${PORT} -v ON_ERROR_STOP=1" <<'EOF'
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sensing') THEN
    CREATE ROLE sensing WITH LOGIN SUPERUSER PASSWORD 'sensing';
  END IF;
END $$;
SELECT 'CREATE DATABASE sensing OWNER sensing'      WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'sensing')      \gexec
SELECT 'CREATE DATABASE sensing_test OWNER sensing' WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'sensing_test') \gexec
SELECT 'CREATE DATABASE sensing_it OWNER sensing'   WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'sensing_it')   \gexec
EOF

su postgres -c "psql -p ${PORT} -d sensing -c 'CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pgcrypto;'"
echo
echo "PostgreSQL ${PG} + pgvector 준비 완료: localhost:${PORT}, 계정 sensing/sensing, DB sensing·sensing_test·sensing_it"
echo "WSL을 다시 시작한 뒤에는 'wsl -d Ubuntu -- sudo service postgresql start' 로 다시 켠다."
