"""환경 변수 기반 설정."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    db_url: str = field(default_factory=lambda: os.getenv(
        "SENSING_DB_DSN", "postgresql://sensing:sensing@localhost:5432/sensing"))
    worker_id: str = field(default_factory=lambda: os.getenv(
        "SENSING_WORKER_ID", f"{socket.gethostname()}-{os.getpid()}"))
    # 작업 임대 시간. 워커가 죽으면 스케줄러가 이 시간이 지난 작업을 회수한다.
    lease_minutes: int = field(default_factory=lambda: int(os.getenv("SENSING_LEASE_MINUTES", "30")))
    poll_seconds: float = field(default_factory=lambda: float(os.getenv("SENSING_POLL_SECONDS", "5")))
    user_agent: str = field(default_factory=lambda: os.getenv(
        "SENSING_USER_AGENT", "SensingHubBot/0.1 (+contact: sensing-hub admin)"))
    # 같은 도메인에 대한 최소 요청 간격(초)
    min_interval_seconds: float = field(default_factory=lambda: float(os.getenv("SENSING_MIN_INTERVAL", "2")))
    # 켜면 도메인 간격을 DB(domain_throttle)로 워커끼리 공유한다
    shared_throttle: bool = field(default_factory=lambda: _bool("SENSING_SHARED_THROTTLE", True))
    connect_timeout: float = field(default_factory=lambda: float(os.getenv("SENSING_CONNECT_TIMEOUT", "10")))
    read_timeout: float = field(default_factory=lambda: float(os.getenv("SENSING_READ_TIMEOUT", "30")))
    max_retries: int = field(default_factory=lambda: int(os.getenv("SENSING_MAX_RETRIES", "3")))
    max_body_bytes: int = field(default_factory=lambda: int(os.getenv("SENSING_MAX_BODY_BYTES", str(5 * 1024 * 1024))))
    # 실행 1회당 상한
    max_pages_per_run: int = field(default_factory=lambda: int(os.getenv("SENSING_MAX_PAGES", "60")))
    max_articles_per_run: int = field(default_factory=lambda: int(os.getenv("SENSING_MAX_ARTICLES", "40")))
    # 수집량 급감 감지: 기사 후보 수가 평소(같은 설정 버전의 최근 정상 실행 평균)의 이 비율 미만이면 이상으로 본다
    anomaly_drop_ratio: float = field(default_factory=lambda: float(os.getenv("SENSING_ANOMALY_RATIO", "0.3")))
    # 이상이 이 횟수만큼 연속되면 소스를 BROKEN으로 바꾼다
    anomaly_broken_after: int = field(default_factory=lambda: int(os.getenv("SENSING_ANOMALY_BROKEN_AFTER", "2")))
    # 테스트에서만 켠다: 사설 IP·localhost 접속 허용
    allow_private_hosts: bool = field(default_factory=lambda: _bool("SENSING_ALLOW_PRIVATE_HOSTS", False))
    evidence_dir: str = field(default_factory=lambda: os.getenv("SENSING_EVIDENCE_DIR", "./data/evidence"))
