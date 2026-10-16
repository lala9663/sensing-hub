#!/usr/bin/env bash
# 로컬 개발용 PostgreSQL 16 + pgvector를 WSL Ubuntu 안에 설치한다 (Docker·Windows 관리자 권한 없이).
#
#   wsl -d Ubuntu -- sudo bash /mnt/c/<이 저장소 경로>/scripts/dev/wsl-postgres.sh
#
# - 포트 5433 (Windows에 설치된 PostgreSQL이 5432를 쓰는 경우가 있어 피한다)
# - 계정 sensing/sensing (개발용 superuser: Flyway가 CREATE EXTENSION vector/pgcrypto를 실행한다)
# - 데이터베이스 sensing(앱), sensing_test(워커 테스트), sensing_it(백엔드 통합 테스트)
# - Windows 쪽에서는 localhost:5433 으로 붙는다 (WSL2 localhost 포워딩). WSL NAT 안에서만 열린다.
#
# 쓰는 쪽 설정:
#   backend: SENSING_DB_URL=jdbc:postgresql://localhost:5433/sensing  (테스트: SENSING_IT_DB_URL=jdbc:postgresql://localhost:5433/sensing_it)
#   worker : SENSING_DB_DSN=postgresql://sensing:sensing@localhost:5433/sensing (테스트: SENSING_TEST_DSN=...:5433/sensing_test)
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
