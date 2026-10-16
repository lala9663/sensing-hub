"""PostgreSQL을 쓰는 통합 테스트: 백엔드가 만든 등록 작업을 워커가 끝까지 처리하는지 본다.

빈 테스트용 DB를 가리키는 SENSING_TEST_DSN이 있을 때만 실행한다. 스키마는 백엔드의 Flyway 파일을 그대로 쓴다.
    SENSING_TEST_DSN=postgresql://sensing:sensing@localhost:5432/sensing_test pytest
"""

from __future__ import annotations

import os
import pathlib
from urllib.parse import urlsplit

import psycopg
import pytest

from fakellm import FakeLlm, portal_recipe
from fakesite import (Site, blocked_site_routes, gov_site_routes, news_site_routes, portal_cms_routes,
                      robots_blocked_routes)
from sensing_worker.config import Settings
from sensing_worker.db import Store
from sensing_worker.fetcher import PoliteFetcher
from sensing_worker.jobs import JobRunner
from sensing_worker.main import process_one
from sensing_worker.throttle import DbThrottle

DSN = os.getenv("SENSING_TEST_DSN")
MIGRATIONS = pathlib.Path(__file__).resolve().parents[2] / "backend/src/main/resources/db/migration"
pytestmark = pytest.mark.skipif(not DSN, reason="SENSING_TEST_DSN not set")


@pytest.fixture()
def env(tmp_path):
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        # Flyway와 같은 순서(V1, V2, ...)로 적용한다
        for path in sorted(MIGRATIONS.glob("V*__*.sql"), key=lambda p: int(p.name[1:].split("__")[0])):
            c.execute(path.read_text(encoding="utf-8"))
    settings = Settings(db_url=DSN, min_interval_seconds=0, max_retries=1, allow_private_hosts=True,
                        worker_id="test-worker", evidence_dir=str(tmp_path / "evidence"))
    store = Store(DSN)
    fetcher = PoliteFetcher(settings)
    runner = JobRunner(store, fetcher, settings)
    sites: list[Site] = []
    yield store, runner, settings, sites
    for s in sites:
        s.close()
    fetcher.close()
    store.close()


def register(store: Store, base_url: str) -> int:
    """백엔드 RegistrationService.startOnboarding과 같은 행을 만든다."""
    domain = base_url.split("://", 1)[1].split(":")[0]
    c = store.conn
    site_id = c.execute("INSERT INTO site (domain, base_url) VALUES (%s, %s) "
                        "ON CONFLICT (domain) DO UPDATE SET base_url = EXCLUDED.base_url RETURNING id",
                        (domain, base_url)).fetchone()["id"]
    source_id = c.execute("INSERT INTO source (site_id, name, requested_by) VALUES (%s, %s, 2) RETURNING id",
                          (site_id, domain)).fetchone()["id"]
    c.execute("INSERT INTO registration_request (user_id, input_url, normalized_domain, status, step, source_id) "
              "VALUES (2, %s, %s, 'PROCESSING', 'QUEUED', %s)", (base_url, domain, source_id))
    c.execute("INSERT INTO crawl_job (source_id, job_type, trigger) VALUES (%s, 'ONBOARD', 'REGISTRATION')", (source_id,))
    return source_id


def q(store: Store, sql: str, *args):
    return store.conn.execute(sql, args).fetchone()


def test_news_site_is_onboarded_automatically(env):
    store, runner, settings, sites = env
    site = Site(news_site_routes()); sites.append(site)
    source_id = register(store, site.base)

    assert process_one(store, runner, settings) == "SUCCESS"

    src = q(store, "SELECT s.status, s.method, s.interval_minutes, t.site_type, v.config, v.version "
                   "FROM source s JOIN site t ON t.id = s.site_id "
                   "JOIN source_config_version v ON v.id = s.active_config_version_id WHERE s.id = %s", source_id)
    assert src["status"] == "ACTIVE" and src["method"] == "STATIC" and src["version"] == 1
    assert src["site_type"] == "NEWS" and src["interval_minutes"] == 60
    assert src["config"]["article_pattern"]["signature"] == "/news/articleView.html?idxno={n}"
    assert "포토" in src["config"]["excluded_sections"]
    assert src["config"]["store_scope"] == "TITLE_LINK"

    docs = store.conn.execute("SELECT title, body_text, published_at FROM document ORDER BY url").fetchall()
    assert len(docs) == 6
    assert docs[0]["title"].startswith("기사 제목") and docs[0]["body_text"] is None  # 언론사는 본문 미저장
    reg = q(store, "SELECT status, step, result_message FROM registration_request")
    assert reg["status"] == "COMPLETED" and "6건" in reg["result_message"]
    assert not any("/news/articleList.html?sc_section_code=S1N9" in h for h in site.hits)  # 포토 섹션은 방문 안 함


def test_second_crawl_adds_only_new_articles(env):
    store, runner, settings, sites = env
    site = Site(news_site_routes(range(101, 104))); sites.append(site)
    source_id = register(store, site.base)
    process_one(store, runner, settings)

    site.routes.update(news_site_routes(range(101, 106)))  # 새 기사 2건
    store.conn.execute("INSERT INTO crawl_job (source_id, job_type, trigger) VALUES (%s, 'CRAWL', 'MANUAL')", (source_id,))
    site.hits.clear()
    assert process_one(store, runner, settings) == "SUCCESS"
    job = q(store, "SELECT found_count, new_count FROM crawl_job ORDER BY id DESC LIMIT 1")
    assert job["found_count"] == 5 and job["new_count"] == 2
    assert sum(1 for h in site.hits if "articleView" in h) == 2  # 이미 본 기사는 다시 열지 않음


def test_public_site_uses_rss_and_keeps_body_and_attachments(env):
    store, runner, settings, sites = env
    site = Site({}); sites.append(site)
    site.routes.update({k: (s, t, b.replace("{base}", site.base)) for k, (s, t, b) in gov_site_routes().items()})
    register(store, site.base)
    # 127.0.0.1은 공공 도메인이 아니므로 유형을 강제로 PUBLIC으로 보지 않는다 → 기본(ETC)은 본문 저장
    assert process_one(store, runner, settings) == "SUCCESS"
    src = q(store, "SELECT s.method, v.config FROM source s JOIN source_config_version v ON v.id = s.active_config_version_id")
    assert src["method"] == "RSS"
    assert q(store, "SELECT count(*) AS c FROM document")["c"] == 5
    doc = q(store, "SELECT url, body_text FROM document ORDER BY url LIMIT 1")
    assert doc["url"].endswith("/board/view.do?nttId=1") and len(doc["body_text"]) > 200  # utm 파라미터 제거
    assert q(store, "SELECT count(*) AS c FROM attachment")["c"] == 5


def test_bot_challenge_site_is_rejected_and_blocked(env):
    store, runner, settings, sites = env
    site = Site(blocked_site_routes()); sites.append(site)
    source_id = register(store, site.base)
    assert process_one(store, runner, settings) == "FAILED"
    assert q(store, "SELECT status FROM source WHERE id = %s", source_id)["status"] == "BLOCKED"
    reg = q(store, "SELECT status, result_message FROM registration_request")
    assert reg["status"] == "REJECTED" and "봇 확인" in reg["result_message"]
    err = q(store, "SELECT code, evidence_path FROM crawl_error")
    assert err["code"] == "BOT_CHALLENGE"
    saved = pathlib.Path(settings.evidence_dir) / err["evidence_path"]   # 차단 화면을 증거로 남긴다
    assert "cf-browser-verification" in saved.read_text(encoding="utf-8")
    assert "code: BOT_CHALLENGE" in saved.read_text(encoding="utf-8")


def test_robots_disallowed_site_is_rejected(env):
    store, runner, settings, sites = env
    site = Site(robots_blocked_routes()); sites.append(site)
    register(store, site.base)
    assert process_one(store, runner, settings) == "FAILED"
    assert q(store, "SELECT code FROM crawl_error")["code"] == "ROBOTS_DISALLOWED"
    assert q(store, "SELECT status FROM registration_request")["status"] == "REJECTED"


def test_site_without_articles_goes_to_admin_review(env):
    store, runner, settings, sites = env
    site = Site({"/": (200, "text/html", "<html><body><a href='/about'>소개</a></body></html>"),
                 "/robots.txt": (404, "text/plain", "")}); sites.append(site)
    source_id = register(store, site.base)
    assert process_one(store, runner, settings) == "FAILED"
    assert q(store, "SELECT status FROM source WHERE id = %s", source_id)["status"] == "REVIEW"
    assert q(store, "SELECT status FROM registration_request")["status"] == "REVIEW"


def test_structure_change_marks_source_broken(env):
    store, runner, settings, sites = env
    site = Site(news_site_routes()); sites.append(site)
    source_id = register(store, site.base)
    process_one(store, runner, settings)

    # 개편: 기사 링크 형식이 바뀌어 기존 패턴과 맞는 링크가 없다
    for key in ("/", "/news/articleList.html"):
        site.routes[key] = (200, "text/html", "<html><body><a href='/article/2026/1'>새 형식</a></body></html>")
    store.conn.execute("INSERT INTO crawl_job (source_id, job_type, trigger) VALUES (%s, 'CRAWL', 'SCHEDULE')", (source_id,))
    assert process_one(store, runner, settings) == "FAILED"
    src = q(store, "SELECT status, status_reason, consecutive_failures FROM source WHERE id = %s", source_id)
    assert src["status"] == "BROKEN" and src["status_reason"] == "PARSE_EMPTY" and src["consecutive_failures"] == 1
    err = q(store, "SELECT evidence_path FROM crawl_error WHERE code = 'PARSE_EMPTY'")
    assert "/article/2026/1" in (pathlib.Path(settings.evidence_dir) / err["evidence_path"]).read_text(encoding="utf-8")


def test_only_one_worker_gets_a_job(env):
    store, runner, settings, sites = env
    site = Site(news_site_routes()); sites.append(site)
    register(store, site.base)
    other = Store(DSN)
    try:
        first = store.claim_job("w1", 30)
        second = other.claim_job("w2", 30)
        assert first is not None and second is None
    finally:
        other.close()


def test_domain_interval_is_shared_between_workers(env):
    # 워커 두 대가 같은 도메인에 번갈아 요청해도 슬롯은 순서대로 interval씩 밀린다
    a, b = DbThrottle(DSN, 30), DbThrottle(DSN, 30)
    try:
        assert a.reserve("www.example.com") == 0
        assert 29 < b.reserve("example.com") <= 30          # www. 유무와 관계없이 같은 도메인
        assert 59 < a.reserve("EXAMPLE.com") <= 60
        assert b.reserve("other.example.org") == 0           # 다른 도메인은 기다리지 않는다
    finally:
        a.close()
        b.close()


def crawl(store: Store, runner, settings, source_id: int, trigger: str = "SCHEDULE") -> str:
    store.conn.execute("INSERT INTO crawl_job (source_id, job_type, trigger) VALUES (%s, 'CRAWL', %s)",
                       (source_id, trigger))
    return process_one(store, runner, settings)


def shrink_hubs(site: Site, article_ids) -> None:
    shrunk = news_site_routes(article_ids)
    for key in ("/", "/news/articleList.html"):
        site.routes[key] = shrunk[key]


def test_sudden_drop_twice_marks_source_broken_and_recheck_restores(env):
    store, runner, settings, sites = env
    site = Site(news_site_routes(range(101, 111))); sites.append(site)
    source_id = register(store, site.base)
    assert process_one(store, runner, settings) == "SUCCESS"            # 온보딩: 후보 10건
    assert crawl(store, runner, settings, source_id) == "SUCCESS"
    assert crawl(store, runner, settings, source_id) == "SUCCESS"       # 기준 3회 확보 (평균 10건)

    # 일부 개편: 목록에 기사 2건만 보인다 (0건이 아니라 PARSE_EMPTY로는 안 잡힌다)
    normal = dict(site.routes)
    shrink_hubs(site, range(101, 103))

    assert crawl(store, runner, settings, source_id) == "PARTIAL"
    src = q(store, "SELECT status, consecutive_anomalies FROM source WHERE id = %s", source_id)
    assert src["status"] == "ACTIVE" and src["consecutive_anomalies"] == 1    # 1회는 지켜본다
    err = q(store, "SELECT code, message FROM crawl_error ORDER BY id DESC LIMIT 1")
    assert err["code"] == "ANOMALY_DROP" and "2건" in err["message"]

    assert crawl(store, runner, settings, source_id) == "PARTIAL"
    src = q(store, "SELECT status, status_reason FROM source WHERE id = %s", source_id)
    assert src["status"] == "BROKEN" and src["status_reason"] == "ANOMALY_DROP"

    # 사이트가 복구된 뒤 관리자 재점검(수동 CRAWL)이 정상이면 ACTIVE로 돌아간다
    site.routes.update(normal)
    assert crawl(store, runner, settings, source_id, "MANUAL") == "SUCCESS"
    src = q(store, "SELECT status, consecutive_anomalies FROM source WHERE id = %s", source_id)
    assert src["status"] == "ACTIVE" and src["consecutive_anomalies"] == 0


def test_no_anomaly_without_enough_history(env):
    store, runner, settings, sites = env
    site = Site(news_site_routes(range(101, 111))); sites.append(site)
    source_id = register(store, site.base)
    process_one(store, runner, settings)
    shrink_hubs(site, range(101, 102))
    assert crawl(store, runner, settings, source_id) == "SUCCESS"    # 기준 이력 1회뿐 → 판단 보류


def test_llm_recipe_is_saved_as_config_and_used_by_first_crawl(env):
    store, runner, settings, sites = env
    site = Site(portal_cms_routes()); sites.append(site)
    llm = FakeLlm([portal_recipe(site.base)])
    runner = JobRunner(store, runner.f, settings, evidence=runner.evidence, llm=llm)   # 생성자 주입
    source_id = register(store, site.base)

    assert process_one(store, runner, settings) == "SUCCESS"

    src = q(store, "SELECT s.status, s.method, v.config, v.change_note FROM source s "
                   "JOIN source_config_version v ON v.id = s.active_config_version_id WHERE s.id = %s", source_id)
    assert src["status"] == "ACTIVE" and src["method"] == "STATIC"
    cfg = src["config"]
    assert cfg["recipe_source"] == "llm" and cfg["llm_model"] == "fake fake-1"
    assert cfg["selectors"] == {"title": ".headline-area strong", "date": ".reg", "body": ".cont-area"}
    assert cfg["date_format"] == "%Y.%m.%d" and cfg["recipe_notes"].startswith("보도자료 목록이 허브")
    assert cfg["hubs"] == [f"{site.base}/portal/bbs/list.do?bbsId=B01"]
    assert src["change_note"].startswith("LLM 레시피로 생성") and "fake fake-1" in src["change_note"]
    assert "토큰 입력 1,000" in src["change_note"]

    docs = store.conn.execute("SELECT title, published_at, body_text FROM document ORDER BY url").fetchall()
    assert len(docs) == 6
    assert all(d["title"].startswith("포털 보도자료") and d["published_at"] is not None for d in docs)   # 셀렉터로 뽑은 제목·날짜
    assert all(len(d["body_text"]) > 200 for d in docs)
    reg = q(store, "SELECT status, result_message FROM registration_request")
    assert reg["status"] == "COMPLETED" and "6건" in reg["result_message"]
    assert len(llm.calls) == 1


def test_same_site_goes_to_review_without_llm(env):
    store, runner, settings, sites = env
    site = Site(portal_cms_routes()); sites.append(site)
    source_id = register(store, site.base)
    assert process_one(store, runner, settings) == "FAILED"                    # NoLlm: 날짜를 못 찾아 품질 불합격
    assert q(store, "SELECT status, status_reason FROM source WHERE id = %s", source_id)["status_reason"] == "QUALITY_FAILED"
    assert q(store, "SELECT status FROM registration_request")["status"] == "REVIEW"


def test_broken_section_keeps_crawl_partial_and_source_active(env):
    store, runner, settings, sites = env
    site = Site(news_site_routes()); sites.append(site)
    source_id = register(store, site.base)
    assert process_one(store, runner, settings) == "SUCCESS"

    # 섹션 하나가 사라져 404: 홈과 다른 섹션은 그대로라 주기 수집은 계속되어야 한다
    hubs = q(store, "SELECT v.config->'hubs' AS hubs FROM source s "
                    "JOIN source_config_version v ON v.id = s.active_config_version_id WHERE s.id = %s", source_id)["hubs"]
    gone = next(h for h in hubs if "sc_section_code=S1N1" in h)
    parts = urlsplit(gone)
    site.routes[parts.path + "?" + parts.query] = (404, "text/html", "<html><body>section removed</body></html>")
    site.hits.clear()

    assert crawl(store, runner, settings, source_id) == "PARTIAL"
    src = q(store, "SELECT status, consecutive_failures FROM source WHERE id = %s", source_id)
    assert src["status"] == "ACTIVE" and src["consecutive_failures"] == 0
    job = q(store, "SELECT status, found_count, error_count FROM crawl_job WHERE job_type = 'CRAWL'")
    assert job["status"] == "PARTIAL" and job["found_count"] == 6 and job["error_count"] == 1
    errs = store.conn.execute("SELECT code, stage, http_status, url FROM crawl_error").fetchall()
    assert len(errs) == 1
    assert errs[0]["code"] == "HTTP_4XX" and errs[0]["stage"] == "LIST" and errs[0]["http_status"] == 404
    assert errs[0]["url"] == gone
    assert any("sc_section_code=S1N2" in h for h in site.hits)  # 다음 허브는 계속 방문했다


# ---------------------------------------------------------------------- 공개 API 소스 (M2, DART)

SECRET_KEY = "flow-test-secret"


def api_runner(store: Store, settings: Settings, handler, secret_key: str = SECRET_KEY) -> tuple[JobRunner, Settings]:
    """가짜 DART(MockTransport)를 쓰는 러너. 실제 opendart에는 요청하지 않는다."""
    import httpx
    s = Settings(**{**settings.__dict__, "secret_key": secret_key})
    runner = JobRunner(store, PoliteFetcher(s, transport=httpx.MockTransport(handler)), s)
    return runner, s


def put_credential(store: Store, provider: str, secret: str, key: str = SECRET_KEY) -> None:
    """백엔드 ApiCredentialService.save와 같은 행 (pgp_sym_encrypt)."""
    store.conn.execute(
        "INSERT INTO api_credential (provider, label, secret_enc, status) VALUES (%s, %s, pgp_sym_encrypt(%s, %s), 'UNVERIFIED') "
        "ON CONFLICT (provider) DO UPDATE SET secret_enc = EXCLUDED.secret_enc, status = 'UNVERIFIED', last_error = NULL",
        (provider, "테스트 키", secret, key))


def test_dart_registration_waits_for_api_key_then_collects(env):
    from fakedart import GOOD_KEY, dart_handler
    store, _, settings, _ = env
    handler = dart_handler()
    runner, s = api_runner(store, settings, handler)
    source_id = register(store, "https://dart.fss.or.kr")

    # ① 키가 없다 → 크롤링 없이 관리자 큐(REVIEW/API_KEY_REQUIRED)로
    assert process_one(store, runner, s) == "FAILED"
    assert handler.calls == []
    src = q(store, "SELECT s.status, s.status_reason, t.risk_flags, t.site_type FROM source s JOIN site t ON t.id = s.site_id "
                   "WHERE s.id = %s", source_id)
    assert src["status"] == "REVIEW" and src["status_reason"] == "API_KEY_REQUIRED"
    assert src["risk_flags"] == {"api_provider": "dart"} and src["site_type"] == "PUBLIC"
    reg = q(store, "SELECT status, step, result_message FROM registration_request")
    assert reg["status"] == "REVIEW" and "공시 API로 받습니다" in reg["result_message"] and "opendart" in reg["result_message"]
    err = q(store, "SELECT code, stage, evidence_path FROM crawl_error")
    assert err["code"] == "API_KEY_REQUIRED" and err["stage"] == "PROBE" and err["evidence_path"] is None

    # ② 관리자가 키를 넣고(백엔드가 암호화 저장 + ONBOARD 재등록) → 검증 → API 설정 → 첫 수집
    put_credential(store, "dart", GOOD_KEY)
    store.conn.execute("INSERT INTO crawl_job (source_id, job_type, trigger) VALUES (%s, 'ONBOARD', 'MANUAL')", (source_id,))
    assert process_one(store, runner, s) == "SUCCESS"
    src = q(store, "SELECT s.status, s.status_reason, s.method, s.interval_minutes, v.config, v.version, v.change_note "
                   "FROM source s JOIN source_config_version v ON v.id = s.active_config_version_id WHERE s.id = %s", source_id)
    assert src["status"] == "ACTIVE" and src["status_reason"] is None and src["method"] == "API"
    assert src["interval_minutes"] == 360 and src["version"] == 1
    assert src["config"] == {"method": "API", "provider": "dart", "days": 1, "corp_codes": []}
    assert "키 검증 통과" in src["change_note"]
    docs = store.conn.execute("SELECT url, title, author, section, published_at, body_text FROM document ORDER BY url").fetchall()
    assert len(docs) == 3 and all(d["body_text"] is None for d in docs)
    assert docs[0]["url"] == "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20261015000001"
    assert docs[0]["author"] == "테스트전자" and docs[0]["published_at"] is not None
    cred = q(store, "SELECT status, last_error, last_verified_at FROM api_credential WHERE provider = 'dart'")
    assert cred["status"] == "VALID" and cred["last_error"] is None and cred["last_verified_at"] is not None
    reg = q(store, "SELECT status, result_message FROM registration_request")
    assert reg["status"] == "COMPLETED" and "3건" in reg["result_message"] and "360분" in reg["result_message"]
    assert all(GOOD_KEY not in (r["message"] or "") + (r["url"] or "")
               for r in store.conn.execute("SELECT message, url FROM crawl_error").fetchall())

    # 주기 수집: 같은 공시는 다시 저장하지 않는다
    assert crawl(store, runner, s, source_id) == "SUCCESS"
    job = q(store, "SELECT found_count, new_count FROM crawl_job ORDER BY id DESC LIMIT 1")
    assert job["found_count"] == 3 and job["new_count"] == 0
    assert q(store, "SELECT count(*) AS c FROM document")["c"] == 3

    # ③ 키가 폐기됐다 → 주기 수집이 REVIEW/API_KEY_INVALID로 보내고 자격 증명은 INVALID
    put_credential(store, "dart", "revoked-key")
    assert crawl(store, runner, s, source_id) == "FAILED"
    src = q(store, "SELECT status, status_reason FROM source WHERE id = %s", source_id)
    assert src["status"] == "REVIEW" and src["status_reason"] == "API_KEY_INVALID"
    cred = q(store, "SELECT status, last_error FROM api_credential WHERE provider = 'dart'")
    assert cred["status"] == "INVALID" and "010" in cred["last_error"] and "revoked-key" not in cred["last_error"]
    err = q(store, "SELECT code, url, message FROM crawl_error ORDER BY id DESC LIMIT 1")
    assert err["code"] == "API_KEY_INVALID" and "crtfc_key=***" in err["url"] and "revoked-key" not in err["message"]


def test_dart_onboard_with_wrong_key_marks_credential_invalid(env):
    from fakedart import dart_handler
    store, _, settings, _ = env
    runner, s = api_runner(store, settings, dart_handler())
    put_credential(store, "dart", "typo-key")
    source_id = register(store, "https://opendart.fss.or.kr")
    assert process_one(store, runner, s) == "FAILED"
    src = q(store, "SELECT status, status_reason FROM source WHERE id = %s", source_id)
    assert src["status"] == "REVIEW" and src["status_reason"] == "API_KEY_INVALID"
    assert q(store, "SELECT status FROM api_credential")["status"] == "INVALID"
    reg = q(store, "SELECT status, result_message FROM registration_request")
    assert reg["status"] == "REVIEW" and "유효하지 않습니다" in reg["result_message"] and "typo-key" not in reg["result_message"]
    assert q(store, "SELECT count(*) AS c FROM source_config_version")["c"] == 0


def test_dart_without_secret_key_or_with_mismatched_key_goes_to_review(env):
    from fakedart import GOOD_KEY, dart_handler
    store, _, settings, _ = env
    put_credential(store, "dart", GOOD_KEY, key="backend-uses-another-key")
    source_id = register(store, "https://dart.fss.or.kr")

    runner, s = api_runner(store, settings, dart_handler(), secret_key="")        # 워커에 대칭키가 없다
    assert process_one(store, runner, s) == "FAILED"
    assert q(store, "SELECT status_reason FROM source WHERE id = %s", source_id)["status_reason"] == "API_KEY_REQUIRED"
    assert "SENSING_SECRET_KEY" in q(store, "SELECT result_message FROM registration_request")["result_message"]

    runner, s = api_runner(store, settings, dart_handler())                       # 대칭키가 백엔드와 다르다 → 복호화 실패
    store.conn.execute("INSERT INTO crawl_job (source_id, job_type, trigger) VALUES (%s, 'ONBOARD', 'MANUAL')", (source_id,))
    assert process_one(store, runner, s) == "FAILED"
    assert q(store, "SELECT status_reason FROM source WHERE id = %s", source_id)["status_reason"] == "API_KEY_REQUIRED"
    assert q(store, "SELECT count(*) AS c FROM document")["c"] == 0


# ---------------------------------------------------------------- 키워드 감시 (M2, 네이버 뉴스 검색)

def register_watch(store: Store, name: str, keywords: list[str], exclude: list[str] | None = None, user_id: int = 2) -> int:
    """백엔드 WatchService.create와 같은 행: site(news.naver.com) + source(kind WATCH) + 설정 v1 + 등록 요청 + ONBOARD."""
    import json as _json
    c = store.conn
    site_id = c.execute("INSERT INTO site (domain, base_url, risk_flags) VALUES (%s, %s, %s::jsonb) "
                        "ON CONFLICT (domain) DO UPDATE SET domain = EXCLUDED.domain RETURNING id",
                        ("news.naver.com", "https://news.naver.com", '{"api_provider":"naver_news"}')).fetchone()["id"]
    source_id = c.execute(
        "INSERT INTO source (site_id, name, kind, watch_name, method, status, interval_minutes, requested_by) "
        "VALUES (%s, %s, 'WATCH', %s, 'API', 'PENDING', 60, %s) RETURNING id",
        (site_id, "watch:" + name, name, user_id)).fetchone()["id"]
    config = {"method": "API", "provider": "naver_news", "keywords": keywords, "exclude": exclude or []}
    config_id = c.execute(
        "INSERT INTO source_config_version (source_id, version, config, status, change_note, created_by) "
        "VALUES (%s, 1, %s::jsonb, 'ACTIVE', '키워드 감시 생성', %s) RETURNING id",
        (source_id, _json.dumps(config, ensure_ascii=False), user_id)).fetchone()["id"]
    c.execute("UPDATE source SET active_config_version_id = %s WHERE id = %s", (config_id, source_id))
    c.execute("INSERT INTO registration_request (user_id, input_url, normalized_domain, status, step, source_id) "
              "VALUES (%s, %s, 'news.naver.com', 'PROCESSING', 'QUEUED', %s)", (user_id, "watch:" + name, source_id))
    c.execute("INSERT INTO crawl_job (source_id, job_type, trigger) VALUES (%s, 'ONBOARD', 'REGISTRATION')", (source_id,))
    return source_id


def test_keyword_watch_waits_for_naver_key_then_collects_titles_and_hits(env):
    from fakenaver import GOOD_KEY, GOOD_SECRET, article, articles, naver_handler
    store, _, settings, _ = env
    handler = naver_handler({"메타넷": articles("메타넷", 3) + [article("메타넷", 4, title="<b>메타넷</b> 채용 공고")],
                             "경쟁사": articles("경쟁사", 2)})
    runner, s = api_runner(store, settings, handler)
    source_id = register_watch(store, "자사 보도", ["메타넷", "경쟁사"], exclude=["채용"])

    # ① 키가 없다 → 호출 없이 관리자 큐(REVIEW/API_KEY_REQUIRED)
    assert process_one(store, runner, s) == "FAILED"
    assert handler.calls == []
    src = q(store, "SELECT s.status, s.status_reason, s.kind, t.risk_flags, t.site_type FROM source s "
                   "JOIN site t ON t.id = s.site_id WHERE s.id = %s", source_id)
    assert src["status"] == "REVIEW" and src["status_reason"] == "API_KEY_REQUIRED" and src["kind"] == "WATCH"
    assert src["risk_flags"] == {"api_provider": "naver_news"} and src["site_type"] == "NEWS"
    reg = q(store, "SELECT status, result_message FROM registration_request WHERE source_id = %s", source_id)
    assert reg["status"] == "REVIEW" and "client_id:client_secret" in reg["result_message"]

    # ② 관리자가 네이버 키를 넣는다 → 검증 → 기존 설정(키워드) 그대로 ACTIVE → 첫 수집 (제목·링크만, 감시 기록)
    put_credential(store, "naver_news", GOOD_KEY)
    store.conn.execute("INSERT INTO crawl_job (source_id, job_type, trigger) VALUES (%s, 'ONBOARD', 'MANUAL')", (source_id,))
    assert process_one(store, runner, s) == "SUCCESS"
    src = q(store, "SELECT s.status, s.status_reason, s.method, s.interval_minutes, v.version, v.config "
                   "FROM source s JOIN source_config_version v ON v.id = s.active_config_version_id WHERE s.id = %s", source_id)
    assert src["status"] == "ACTIVE" and src["status_reason"] is None and src["method"] == "API"
    assert src["interval_minutes"] == 60 and src["version"] == 1                   # 새 설정 버전을 만들지 않았다
    assert src["config"]["keywords"] == ["메타넷", "경쟁사"]
    assert q(store, "SELECT count(*) AS c FROM source_config_version WHERE source_id = %s", source_id)["c"] == 1
    docs = store.conn.execute("SELECT url, title, body_text, published_at FROM document ORDER BY id").fetchall()
    assert len(docs) == 5 and all(d["body_text"] is None and d["published_at"] is not None for d in docs)
    assert docs[0]["title"] == '메타넷, "신제품" 공개 1' and "<b>" not in docs[0]["title"]
    assert not any("채용" in d["title"] for d in docs)
    hits = store.conn.execute("SELECT d.url, w.keyword FROM document_watch w JOIN document d ON d.id = w.document_id "
                              "WHERE w.source_id = %s ORDER BY d.id", (source_id,)).fetchall()
    assert [h["keyword"] for h in hits] == ["메타넷"] * 3 + ["경쟁사"] * 2
    cred = q(store, "SELECT status, last_error FROM api_credential WHERE provider = 'naver_news'")
    assert cred["status"] == "VALID" and cred["last_error"] is None
    reg = q(store, "SELECT status, result_message FROM registration_request WHERE source_id = %s", source_id)
    assert reg["status"] == "COMPLETED" and reg["result_message"].startswith("감시 등록 완료") and "5건" in reg["result_message"]
    assert "60분" in reg["result_message"]
    blob = " ".join((r["message"] or "") + (r["url"] or "") for r in store.conn.execute("SELECT message, url FROM crawl_error"))
    assert GOOD_SECRET not in blob

    # 주기 수집: 같은 기사는 다시 저장하지 않고 감시 기록도 그대로
    assert crawl(store, runner, s, source_id) == "SUCCESS"
    job = q(store, "SELECT found_count, new_count FROM crawl_job ORDER BY id DESC LIMIT 1")
    assert job["found_count"] == 5 and job["new_count"] == 0
    assert q(store, "SELECT count(*) AS c FROM document_watch")["c"] == 5


def test_two_watches_share_one_document_with_a_hit_each(env):
    from fakenaver import GOOD_KEY, articles, naver_handler
    store, _, settings, _ = env
    put_credential(store, "naver_news", GOOD_KEY)
    runner, s = api_runner(store, settings, naver_handler({"반도체": articles("반도체", 2)}))
    a = register_watch(store, "자사", ["반도체"])
    b = register_watch(store, "경쟁사", ["반도체"], user_id=3)
    assert process_one(store, runner, s) == "SUCCESS" and process_one(store, runner, s) == "SUCCESS"
    assert q(store, "SELECT count(*) AS c FROM document")["c"] == 2
    hits = store.conn.execute("SELECT source_id, count(*) AS c FROM document_watch GROUP BY source_id ORDER BY source_id").fetchall()
    assert [(h["source_id"], h["c"]) for h in hits] == [(a, 2), (b, 2)]
    assert q(store, "SELECT count(*) AS c FROM document_watch")["c"] == 4
    assert q(store, "SELECT count(DISTINCT domain) AS c FROM site")["c"] == 1   # news.naver.com 한 행을 공유


def test_naver_domain_registered_as_site_asks_for_keyword_watch(env):
    from fakenaver import GOOD_KEY, naver_handler
    store, _, settings, _ = env
    put_credential(store, "naver_news", GOOD_KEY)
    handler = naver_handler()
    runner, s = api_runner(store, settings, handler)
    source_id = register(store, "https://news.naver.com")
    assert process_one(store, runner, s) == "FAILED"
    assert handler.calls == []
    src = q(store, "SELECT status, status_reason, kind FROM source WHERE id = %s", source_id)
    assert src["status"] == "REVIEW" and src["status_reason"] == "KEYWORDS_REQUIRED" and src["kind"] == "SITE"
    reg = q(store, "SELECT status, result_message FROM registration_request")
    assert reg["status"] == "REVIEW" and reg["result_message"] == "네이버 뉴스는 키워드 감시로 등록합니다. 관리자 또는 등록 담당자가 키워드 묶음을 만들어 주세요."
    assert q(store, "SELECT count(*) AS c FROM source_config_version")["c"] == 0


# ---------------------------------------------------------------- 보강 (M2, 요약·분류)

def enrichments(store: Store) -> list[dict]:
    return store.conn.execute("SELECT e.*, d.url FROM document_enrichment e JOIN document d ON d.id = e.document_id "
                              "ORDER BY e.document_id").fetchall()


def gov_site(sites: list[Site]) -> Site:
    site = Site({}); sites.append(site)
    site.routes.update({k: (s, t, b.replace("{base}", site.base)) for k, (s, t, b) in gov_site_routes().items()})
    return site


def test_enrich_pending_classifies_collected_documents_in_batches(env):
    from fakellm import BatchFakeLlm
    from sensing_worker.enrich import enrich_pending
    store, runner, settings, sites = env
    news = Site(news_site_routes()); sites.append(news)                       # 6건, 본문 없음 (언론사)
    gov = gov_site(sites)                                                    # 5건, 본문 있음
    register(store, news.base)
    assert process_one(store, runner, settings) == "SUCCESS"
    register(store, gov.base)
    assert process_one(store, runner, settings) == "SUCCESS"
    assert q(store, "SELECT count(*) AS c FROM document")["c"] == 11

    llm = BatchFakeLlm()
    first = enrich_pending(store, llm, settings, limit=10)
    assert (first.fetched, first.done, first.failed, first.skipped) == (10, 10, 0, 0) and len(llm.calls) == 1
    second = enrich_pending(store, llm, settings, limit=10)                    # 남은 1건
    assert second.fetched == 1 and second.done == 1
    third = enrich_pending(store, llm, settings, limit=10)                     # 더 가져올 것이 없다
    assert third.fetched == 0 and len(llm.calls) == 2

    rows = enrichments(store)
    assert len(rows) == 11 and all(r["status"] == "DONE" and r["document_version"] == 1 for r in rows)
    assert all(r["model"] == "fake fake-1" and r["topics"] == ["정책·규제"] and len(r["tags"]) == 5 for r in rows)
    press = [r for r in rows if "articleView" in r["url"]]
    public = [r for r in rows if "/board/view.do" in r["url"]]
    assert len(press) == 6 and all(r["summary"] is None for r in press)         # 언론 기사: 요약을 저장하지 않는다
    assert len(public) == 5 and all(r["summary"].startswith("요약 ") for r in public)
    assert "본문: (없음 — 언론 기사, 제목만 저장)" in llm.calls[0][1] + llm.calls[1][1]


def test_enrich_runs_again_when_document_body_changes(env):
    from fakellm import BatchFakeLlm
    from sensing_worker.enrich import enrich_pending
    store, runner, settings, sites = env
    gov = gov_site(sites)
    source_id = register(store, gov.base)
    assert process_one(store, runner, settings) == "SUCCESS"
    llm = BatchFakeLlm()
    assert enrich_pending(store, llm, settings, limit=10).done == 5
    assert enrich_pending(store, llm, settings, limit=10).fetched == 0

    doc = q(store, "SELECT id, url, title, version FROM document ORDER BY id LIMIT 1")
    job_id = q(store, "SELECT id FROM crawl_job ORDER BY id LIMIT 1")["id"]
    doc_id, result = store.upsert_document(source_id=source_id, job_id=job_id, url=doc["url"], title=doc["title"],
                                           author=None, section=None, published_at=None, body="수정된 본문 " * 30)
    assert result == "UPDATED" and doc_id == doc["id"]
    assert q(store, "SELECT version FROM document WHERE id = %s", doc_id)["version"] == 2

    outcome = enrich_pending(store, llm, settings, limit=10)                  # 바뀐 문서만 다시
    assert outcome.fetched == 1 and outcome.done == 1
    row = q(store, "SELECT document_version, summary FROM document_enrichment WHERE document_id = %s", doc_id)
    assert row["document_version"] == 2 and row["summary"] == "요약 1"
    assert enrich_pending(store, llm, settings, limit=10).fetched == 0


def test_enrich_failed_rows_are_retried_after_a_day_and_llm_outage_writes_nothing(env):
    from fakellm import BatchFakeLlm
    from sensing_worker.enrich import enrich_pending
    from sensing_worker.llm import LlmUnavailable
    store, runner, settings, sites = env
    news = Site(news_site_routes(range(101, 103))); sites.append(news)
    register(store, news.base)
    assert process_one(store, runner, settings) == "SUCCESS"

    outage = enrich_pending(store, FakeLlm(error=LlmUnavailable("down")), settings, limit=10)
    assert outage.fetched == 2 and outage.llm_unavailable and enrichments(store) == []     # 아무것도 쓰지 않는다

    bad = FakeLlm(['{"items": []}'])                                                        # 검증 실패 2회 → FAILED
    failed = enrich_pending(store, bad, settings, limit=10)
    assert failed.failed == 2 and len(bad.calls) == 2
    rows = enrichments(store)
    assert all(r["status"] == "FAILED" and "검증 실패" in r["error"] and r["topics"] == [] for r in rows)
    assert enrich_pending(store, BatchFakeLlm(), settings, limit=10).fetched == 0          # 바로는 다시 안 잡는다

    store.conn.execute("UPDATE document_enrichment SET created_at = now() - interval '25 hours'")
    retried = enrich_pending(store, BatchFakeLlm(), settings, limit=10)                     # 24시간 뒤에는 다시
    assert retried.done == 2
    assert all(r["status"] == "DONE" and r["error"] is None for r in enrichments(store))


def test_two_workers_do_not_enrich_the_same_documents(env):
    from fakellm import BatchFakeLlm
    from sensing_worker.enrich import enrich_pending
    store, runner, settings, sites = env
    news = Site(news_site_routes(range(101, 104))); sites.append(news)
    register(store, news.base)
    assert process_one(store, runner, settings) == "SUCCESS"

    other = Store(DSN)
    try:
        with store.conn.transaction():                                   # 워커 1이 2건을 잠근 채 LLM을 기다리는 중
            locked = store.lock_pending_enrichment(2)
            assert len(locked) == 2
            outcome = enrich_pending(other, BatchFakeLlm(), settings, limit=10)      # 워커 2는 남은 1건만
            assert outcome.fetched == 1 and outcome.done == 1
            assert {r["document_id"] for r in enrichments(other)}.isdisjoint({r["id"] for r in locked})
        assert enrich_pending(other, BatchFakeLlm(), settings, limit=10).fetched == 2   # 잠금이 풀리면 나머지
        assert len(enrichments(store)) == 3
    finally:
        other.close()
