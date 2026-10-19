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
    # 생존 신호(worker_heartbeat) 간격. 운영 지표는 2분 안에 신호가 있는 워커를 살아 있다고 센다
    heartbeat_seconds: float = field(default_factory=lambda: float(os.getenv("SENSING_HEARTBEAT_SECONDS", "30")))
    user_agent: str = field(default_factory=lambda: os.getenv(
        "SENSING_USER_AGENT", "SensingHubBot/0.1 (+contact: sensing-hub admin)"))
    # 같은 도메인에 대한 최소 요청 간격(초)
    min_interval_seconds: float = field(default_factory=lambda: float(os.getenv("SENSING_MIN_INTERVAL", "2")))
    # 켜면 도메인 간격을 DB(domain_throttle)로 워커끼리 공유한다
    shared_throttle: bool = field(default_factory=lambda: _bool("SENSING_SHARED_THROTTLE", True))
    connect_timeout: float = field(default_factory=lambda: float(os.getenv("SENSING_CONNECT_TIMEOUT", "10")))
    read_timeout: float = field(default_factory=lambda: float(os.getenv("SENSING_READ_TIMEOUT", "30")))
    max_retries: int = field(default_factory=lambda: int(os.getenv("SENSING_MAX_RETRIES", "3")))
    # TLS 핸드셰이크가 실패한 호스트를 호환 컨텍스트(SECLEVEL=1, TLS 1.2+, legacy renegotiation, 인증서 검증 유지)로 한 번 더 시도
    tls_compat: bool = field(default_factory=lambda: _bool("SENSING_TLS_COMPAT", True))
    max_body_bytes: int = field(default_factory=lambda: int(os.getenv("SENSING_MAX_BODY_BYTES", str(5 * 1024 * 1024))))
    # 실행 1회당 상한
    max_pages_per_run: int = field(default_factory=lambda: int(os.getenv("SENSING_MAX_PAGES", "60")))
    max_articles_per_run: int = field(default_factory=lambda: int(os.getenv("SENSING_MAX_ARTICLES", "40")))
    # 사이트 하나에 기사 종류(상세 URL 모양) 여러 개: 등록 때 켜 둘 종류 수 상한(대표 종류 포함)과, 대표 종류 말고 품질 검사할
    # 후보 수 상한. 후보 하나에 표본 5건을 더 연다. 0이면 종류를 늘리지 않는다 (대표 종류 하나)
    max_kinds: int = field(default_factory=lambda: int(os.getenv("SENSING_MAX_KINDS", "7")))
    max_kind_checks: int = field(default_factory=lambda: int(os.getenv("SENSING_MAX_KIND_CHECKS", "8")))
    # 수집량 급감 감지: 기사 후보 수가 평소(같은 설정 버전의 최근 정상 실행 평균)의 이 비율 미만이면 이상으로 본다
    anomaly_drop_ratio: float = field(default_factory=lambda: float(os.getenv("SENSING_ANOMALY_RATIO", "0.3")))
    # 이상이 이 횟수만큼 연속되면 소스를 BROKEN으로 바꾼다
    anomaly_broken_after: int = field(default_factory=lambda: int(os.getenv("SENSING_ANOMALY_BROKEN_AFTER", "2")))
    # 테스트에서만 켠다: 사설 IP·localhost 접속 허용
    allow_private_hosts: bool = field(default_factory=lambda: _bool("SENSING_ALLOW_PRIVATE_HOSTS", False))
    # 실패 증거(응답 HTML 일부) 저장 위치와 보관 기간
    evidence_dir: str = field(default_factory=lambda: os.getenv("SENSING_EVIDENCE_DIR", "./data/evidence"))
    evidence_retention_days: int = field(default_factory=lambda: int(os.getenv("SENSING_EVIDENCE_DAYS", "30")))
    # 공개 API 자격 증명(api_credential.secret_enc)을 푸는 대칭키. 백엔드 sensing.secret-key와 같은 값이어야 한다.
    # 비어 있으면 API 소스는 수집하지 않고 관리자 큐(REVIEW)로 보낸다
    secret_key: str = field(default_factory=lambda: os.getenv("SENSING_SECRET_KEY", ""))
    # API 소스 실행 1회당 받을 항목 수 상한 (DART는 하루 공시가 수백 건이라 기사 상한과 따로 둔다)
    max_api_items_per_run: int = field(default_factory=lambda: int(os.getenv("SENSING_MAX_API_ITEMS", "500")))
    # 네이버 검색 API 일 호출 한도(공식 25,000)보다 낮게 잡은 우리 쪽 상한. 지금은 호출 수를 로그에 남기는 데만 쓴다
    naver_daily_limit: int = field(default_factory=lambda: int(os.getenv("SENSING_NAVER_DAILY_LIMIT", "20000")))
    # 네이버 뉴스 검색 사용 방식 (백엔드 sensing.naver.mode와 같은 값이어야 한다):
    #   display(기본) — 표시 전용. 개정 약관(2026-09-07, 저장·캐싱·AI 입력 금지)에 맞춰 워커는 네이버 결과를 수집·저장하지 않는다
    #                   (감시 화면에서 백엔드가 실시간으로 불러와 보여 주기만 한다)
    #   store        — 저장·분석. 법무 확인 뒤에만 켠다. 네이버 모드 감시(config naver true)를 주기 수집해 document에 저장한다
    #   off          — 네이버를 쓰지 않는다
    naver_mode: str = field(default_factory=lambda: os.getenv("SENSING_NAVER_MODE", "display").strip().lower())
    # 네이버 검색 API 주소: hub(기본, NAVER API HUB naverapihub.apigw.ntruss.com) | developers(예전 개발자센터
    # openapi.naver.com, 2027-06-30 지원 종료). 헤더 이름도 이에 따라 바뀐다
    naver_api: str = field(default_factory=lambda: os.getenv("SENSING_NAVER_API", "hub").strip().lower())
    # 검색 주소를 통째로 바꾼다 (테스트·프록시용). 비우면 naver_api의 공식 주소
    naver_search_url: str = field(default_factory=lambda: os.getenv("SENSING_NAVER_SEARCH_URL", "").strip())
    # 등록 시 LLM이 수집 레시피(허브·기사 패턴·셀렉터)를 만들게 할지. none(기본) | anthropic | openai(OpenAI 호환 API)
    llm_provider: str = field(default_factory=lambda: os.getenv("SENSING_LLM_PROVIDER", "none"))
    llm_model: str = field(default_factory=lambda: os.getenv("SENSING_LLM_MODEL", "claude-opus-5-5"))
    # openai 제공자의 주소 (예: http://localhost:11434/v1, http://vllm:8000/v1). anthropic은 ANTHROPIC_BASE_URL을 쓴다
    llm_base_url: str = field(default_factory=lambda: os.getenv("SENSING_LLM_BASE_URL", ""))
    # openai 제공자의 키. 비어 있으면 Authorization 헤더를 보내지 않는다 (로컬 vLLM·Ollama). anthropic은 ANTHROPIC_API_KEY
    llm_api_key: str = field(default_factory=lambda: os.getenv("SENSING_LLM_API_KEY", ""))
    # anthropic: 안전 분류기가 거절하면 서버가 다른 모델로 대신 답하게 한다
    llm_fallbacks: bool = field(default_factory=lambda: _bool("SENSING_LLM_FALLBACKS", True))
    # LLM에 보여 줄 샘플 기사 페이지 수와 페이지당 글자 수 상한, 요청 제한 시간
    llm_max_sample_pages: int = field(default_factory=lambda: int(os.getenv("SENSING_LLM_MAX_SAMPLE_PAGES", "5")))
    llm_page_chars: int = field(default_factory=lambda: int(os.getenv("SENSING_LLM_PAGE_CHARS", "30000")))
    llm_timeout_seconds: float = field(default_factory=lambda: float(os.getenv("SENSING_LLM_TIMEOUT", "120")))
    # 수집 시점 요약·분류(보강). 큐에 작업이 없을 때 LLM이 설정돼 있으면 한 묶음씩 돌린다
    enrich_enabled: bool = field(default_factory=lambda: _bool("SENSING_ENRICH_ENABLED", True))
    # 한 번의 LLM 호출에 묶는 문서 수
    enrich_batch: int = field(default_factory=lambda: int(os.getenv("SENSING_ENRICH_BATCH", "10")))
    # 문서당 LLM에 보여 줄 본문 글자 수 상한
    enrich_body_chars: int = field(default_factory=lambda: int(os.getenv("SENSING_ENRICH_BODY_CHARS", "4000")))
    # 작업 큐 교대: 등록(ONBOARD)을 이 횟수 집을 때마다 주기 수집(CRAWL)을 한 번 먼저 집는다 (일괄 등록 수백 건이 주기 수집을 오래 막지 않도록).
    # 0이면 예전처럼 ONBOARD를 항상 먼저 집는다
    onboard_ratio: int = field(default_factory=lambda: int(os.getenv("SENSING_ONBOARD_RATIO", "3")))
    # 주기 자동 조정: 최근 adaptive_window회 주기 수집의 새 문서 평균이 adaptive_min_new 미만이면 주기를 두 배로(최대 adaptive_max_interval분),
    # max_articles_per_run의 절반을 넘으면 사이트 유형 기본값으로 되돌린다. 사람이 정한 주기(source.interval_locked)는 건드리지 않는다
    adaptive_interval: bool = field(default_factory=lambda: _bool("SENSING_ADAPTIVE_INTERVAL", True))
    adaptive_window: int = field(default_factory=lambda: int(os.getenv("SENSING_ADAPTIVE_WINDOW", "10")))
    adaptive_min_new: float = field(default_factory=lambda: float(os.getenv("SENSING_ADAPTIVE_MIN_NEW", "0.5")))
    adaptive_max_interval: int = field(default_factory=lambda: int(os.getenv("SENSING_ADAPTIVE_MAX_INTERVAL", "1440")))
    # 링크 모음(포털) 페이지를 등록하면 가리키는 언론사를 대신 등록한다. 한 페이지에서 등록할 언론사 수 상한
    portal_max_outlets: int = field(default_factory=lambda: int(os.getenv("SENSING_PORTAL_MAX_OUTLETS", "50")))
    # 전문 검색 색인과 이슈 묶기 (M5 기반). 큐에 작업이 없을 때 LLM과 무관하게 한 묶음씩 돈다
    index_enabled: bool = field(default_factory=lambda: _bool("SENSING_INDEX_ENABLED", True))
    index_batch: int = field(default_factory=lambda: int(os.getenv("SENSING_INDEX_BATCH", "200")))
    # 이슈 묶기: 이 시간 안의 이슈와만 비교하고, 마지막 문서가 이보다 오래된 이슈는 닫는다
    issue_window_hours: float = field(default_factory=lambda: float(os.getenv("SENSING_ISSUE_WINDOW_HOURS", "72")))
    # 붙이는 기준 (issues.Rule). 2026-10-19 운영 DB 제목 2,998건(53개 언론사)으로 보정했다 — docs/decisions.md
    issue_threshold: float = field(default_factory=lambda: float(os.getenv("SENSING_ISSUE_THRESHOLD", "0.4")))
    issue_min_shared: int = field(default_factory=lambda: int(os.getenv("SENSING_ISSUE_MIN_SHARED", "2")))
    issue_pair_threshold: float = field(default_factory=lambda: float(os.getenv("SENSING_ISSUE_PAIR_THRESHOLD", "0.66")))
    # 의미 검색용 임베딩 (M5 챗봇 2단계, embed.py). none(기본) | openai(OpenAI 호환 /embeddings — Gemini OpenAI 호환 주소 포함).
    # 백엔드(EmbeddingConfig)도 질의를 같은 설정으로 임베딩하므로 두 쪽 값이 같아야 한다
    embed_provider: str = field(default_factory=lambda: os.getenv("SENSING_EMBED_PROVIDER", "none"))
    embed_model: str = field(default_factory=lambda: os.getenv("SENSING_EMBED_MODEL", ""))
    # 비우면 SENSING_LLM_BASE_URL·SENSING_LLM_API_KEY를 함께 쓴다 (Gemini처럼 같은 키·주소). 주소를 따로 주면 키도 따로 준다 —
    # LLM 키를 다른 서버에 보내지 않기 위해서다
    embed_base_url: str = field(default_factory=lambda: os.getenv("SENSING_EMBED_BASE_URL", ""))
    embed_api_key: str = field(default_factory=lambda: os.getenv("SENSING_EMBED_API_KEY", ""))
    # chunk.embedding이 VECTOR(1024)라 1024여야 한다. dimensions를 받는 모델(OpenAI text-embedding-3, Gemini)은 줄여서 주고,
    # 받지 않는 서버는 빼고 다시 보낸다 (그때는 모델 자체가 1024차원이어야 한다, 예: bge-m3)
    embed_dimensions: int = field(default_factory=lambda: int(os.getenv("SENSING_EMBED_DIMENSIONS", "1024")))
    # 호출 한 번에 보내는 조각 수, 분당 보내는 조각 수 상한(0이면 없음 — Gemini 무료 등급은 묶음 안의 글 하나를 요청 하나로 세어
    # 분당 100개), 제한 시간, 실패 뒤 다시 시도하기까지 쉬는 시간(초, 실패가 이어지면 두 배씩 최대 30분)
    embed_batch: int = field(default_factory=lambda: int(os.getenv("SENSING_EMBED_BATCH", "20")))
    embed_per_minute: float = field(default_factory=lambda: float(os.getenv("SENSING_EMBED_PER_MINUTE", "80")))
    embed_timeout_seconds: float = field(default_factory=lambda: float(os.getenv("SENSING_EMBED_TIMEOUT", "30")))
    embed_backoff_seconds: float = field(default_factory=lambda: float(os.getenv("SENSING_EMBED_BACKOFF", "60")))
    # 과거 기사 채우기(BACKFILL). 큐가 빌 때만 한 묶음씩 돈다 — 한 묶음에 보내는 요청 수(사이트맵·목록·기사·robots 포함)
    backfill_enabled: bool = field(default_factory=lambda: _bool("SENSING_BACKFILL_ENABLED", True))
    backfill_chunk: int = field(default_factory=lambda: int(os.getenv("SENSING_BACKFILL_CHUNK", "100")))
    # 채우기 하나가 읽는, 이름에 날짜가 없는 사이트맵 파일 수 상한(이름에 날짜가 있는 파일·번호 묶음의 이분 탐색은 묶음에 필요한 만큼)과,
    # 목록 페이지를 거꾸로 넘길 때 허브 하나에서 볼 쪽 수 상한
    backfill_max_sitemaps: int = field(default_factory=lambda: int(os.getenv("SENSING_BACKFILL_MAX_SITEMAPS", "60")))
    backfill_max_pages: int = field(default_factory=lambda: int(os.getenv("SENSING_BACKFILL_MAX_PAGES", "300")))
