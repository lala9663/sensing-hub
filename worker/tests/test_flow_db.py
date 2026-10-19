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


# ---------------------------------------------------------------- 회사 레이어 (M3): 회사 감시 → 문서 연결, 보강 → NEWS 신호, 점검 소스 → BIZ_STATUS 신호

def register_company(store: Store, name: str, aliases: list[str], biz_no: str | None, user_id: int = 2) -> tuple[int, int]:
    """백엔드 CompanyService.create와 같은 행: company + 회사 감시(WATCH, 360분) + watch_source_id. (company_id, watch_source_id)."""
    import json as _json
    watch_id = register_watch(store, "회사:" + name, [name, *aliases], user_id=user_id)
    store.conn.execute("UPDATE source SET interval_minutes = 360 WHERE id = %s", (watch_id,))
    company_id = store.conn.execute(
        "INSERT INTO company (name, aliases, biz_no, watch_source_id, created_by) VALUES (%s, %s::jsonb, %s, %s, %s) RETURNING id",
        (name, _json.dumps(aliases, ensure_ascii=False), biz_no, watch_id, user_id)).fetchone()["id"]
    return company_id, watch_id


# 점검 종류 → (site 도메인, 제공자, 주기). 백엔드 CompanyService.CHECK_KINDS와 같다
CHECK_KINDS = {
    "nts_status": ("api.odcloud.kr", "data_go_kr", 1440),
    "dart_corp_map": ("opendart.fss.or.kr", "dart", 10080),
    "dart_disclosure": ("opendart.fss.or.kr", "dart", 360),
    "g2b_debarment": ("apis.data.go.kr", "data_go_kr", 1440),
}


def register_check_source(store: Store, check: str = "nts_status", user_id: int = 2) -> int:
    """백엔드 CompanyService.ensureCheckSources와 같은 행: site + source(kind CHECK, "check:<종류>", 종류별 주기) + 설정 v1 + ONBOARD."""
    import json as _json
    domain, provider, interval = CHECK_KINDS[check]
    c = store.conn
    site_id = c.execute("INSERT INTO site (domain, base_url, site_type, risk_flags) VALUES (%s, %s, 'PUBLIC', %s::jsonb) "
                        "ON CONFLICT (domain) DO UPDATE SET domain = EXCLUDED.domain RETURNING id",
                        (domain, "https://" + domain, _json.dumps({"api_provider": provider}))).fetchone()["id"]
    source_id = c.execute(
        "INSERT INTO source (site_id, name, kind, method, status, interval_minutes, requested_by) "
        "VALUES (%s, %s, 'CHECK', 'API', 'PENDING', %s, %s) RETURNING id", (site_id, "check:" + check, interval, user_id)).fetchone()["id"]
    config_id = c.execute(
        "INSERT INTO source_config_version (source_id, version, config, status, change_note, created_by) "
        "VALUES (%s, 1, %s::jsonb, 'ACTIVE', '회사 점검 소스 생성', %s) RETURNING id",
        (source_id, _json.dumps({"method": "API", "provider": provider, "check": check}), user_id)).fetchone()["id"]
    c.execute("UPDATE source SET active_config_version_id = %s WHERE id = %s", (config_id, source_id))
    c.execute("INSERT INTO crawl_job (source_id, job_type, trigger) VALUES (%s, 'ONBOARD', 'REGISTRATION')", (source_id,))
    return source_id


def signals(store: Store, company_id: int | None = None) -> list[dict]:
    where = "WHERE company_id = %s" if company_id else ""
    return store.conn.execute(f"SELECT * FROM company_signal {where} ORDER BY id", (company_id,) if company_id else ()).fetchall()


def test_company_watch_links_articles_and_enrichment_raises_news_signal(env):
    from fakellm import BatchFakeLlm
    from fakenaver import GOOD_KEY, article, articles, naver_handler
    from sensing_worker.enrich import enrich_pending
    store, _, settings, _ = env
    put_credential(store, "naver_news", GOOD_KEY)
    handler = naver_handler({"나부품": [article("나부품", 1, title="<b>나부품</b> 공장 화재로 생산 중단"),
                                     article("나부품", 2, title="<b>나부품</b> 신제품 출시")],
                             "NABU": [article("NABU", 3, title="NABU 대표이사 교체")],
                             "가전자": articles("가전자", 1)})
    runner, s = api_runner(store, settings, handler)
    nabu, nabu_watch = register_company(store, "나부품", ["NABU"], "1234567890")
    ga, ga_watch = register_company(store, "가전자", [], None)
    plain_watch = register_watch(store, "경쟁사", ["가전자"], user_id=3)        # 회사가 아닌 일반 감시

    assert process_one(store, runner, s) == "SUCCESS" and process_one(store, runner, s) == "SUCCESS"
    assert process_one(store, runner, s) == "SUCCESS"
    assert q(store, "SELECT interval_minutes FROM source WHERE id = %s", nabu_watch)["interval_minutes"] == 360   # 회사 감시 주기 유지
    links = store.conn.execute("SELECT dc.company_id, dc.matched_by, d.title FROM document_company dc "
                               "JOIN document d ON d.id = dc.document_id ORDER BY dc.company_id, d.id").fetchall()
    assert [(l["company_id"], l["matched_by"]) for l in links] == [(nabu, "WATCH")] * 3 + [(ga, "WATCH")]
    assert signals(store) == []                                                   # 보강 전에는 신호가 없다

    def classify(idx, prompt):
        topics = ["사건·사고"] if "화재" in _doc_block(prompt, idx) else ["인사·조직"] if "교체" in _doc_block(prompt, idx) else ["자사"]
        return {"idx": idx, "summary": None, "topics": topics, "tags": []}
    outcome = enrich_pending(store, BatchFakeLlm(classify), s, limit=10)
    assert outcome.done == 4 and outcome.company_signals == 2 and outcome.company_links == 0
    rows = signals(store, nabu)
    assert [(r["kind"], r["severity"], r["title"]) for r in rows] == [("NEWS", "MEDIUM", "나부품 공장 화재로 생산 중단"),
                                                                     ("NEWS", "MEDIUM", "NABU 대표이사 교체")]
    assert all(r["document_id"] is not None and len(r["fingerprint"]) == 64 and r["detail"]["matched_by"] == "WATCH" for r in rows)
    assert signals(store, ga) == []                                                # 가전자 기사는 토픽이 자사라 신호 없음

    # 다시 수집해도 연결·신호는 늘지 않는다
    assert crawl(store, runner, s, nabu_watch) == "SUCCESS"
    assert q(store, "SELECT count(*) AS c FROM document_company")["c"] == 4
    assert enrich_pending(store, BatchFakeLlm(classify), s, limit=10).fetched == 0 and len(signals(store)) == 2


def _doc_block(prompt: str, idx: int) -> str:
    start = prompt.index(f"[문서 {idx}]")
    end = prompt.find("[문서 ", start + 1)
    return prompt[start:end if end > 0 else None]


def test_enrichment_tags_link_documents_to_companies_by_exact_name(env):
    from fakellm import BatchFakeLlm
    from sensing_worker.enrich import enrich_pending
    store, runner, settings, sites = env
    gov = gov_site(sites)
    register(store, gov.base)
    assert process_one(store, runner, settings) == "SUCCESS"                    # 공공기관 문서 5건 (회사 감시와 무관)
    nabu, _ = register_company(store, "나부품", ["NABU"], None)
    process_one(store, runner, settings)                                        # 회사 감시 ONBOARD는 키가 없어 REVIEW (상관없음)

    calls = iter([["나부품", "중소벤처기업부"], ["nabu"], ["나부품 협력사"], ["전자"], []])
    tags = {}

    def classify(idx, prompt):
        tags[idx] = next(calls)
        return {"idx": idx, "summary": f"요약 {idx}", "topics": ["사건·사고"], "tags": tags[idx]}
    outcome = enrich_pending(store, BatchFakeLlm(classify), settings, limit=10)
    assert outcome.done == 5 and outcome.company_links == 2 and outcome.company_signals == 2   # 정확 일치 2건만 (대소문자 무시)
    links = store.conn.execute("SELECT matched_by FROM document_company WHERE company_id = %s", (nabu,)).fetchall()
    assert [l["matched_by"] for l in links] == ["TAG", "TAG"]
    assert all(r["detail"]["matched_by"] == "TAG" for r in signals(store, nabu))


def test_check_source_records_biz_status_signals_once_and_waits_for_portal_key(env):
    from fakents import GOOD_KEY, closed, continuing, nts_handler, suspended
    from sensing_worker.apis import catalog
    store, _, settings, _ = env
    nabu, _ = register_company(store, "나부품", [], "1234567890")
    ga, _ = register_company(store, "가전자", [], "1018112345")
    nobiz, _ = register_company(store, "번호없음", [], None)
    check_id = register_check_source(store)
    # 회사 감시 ONBOARD 세 건은 네이버 키가 없어 REVIEW로 빠진다 (이 테스트의 관심사는 점검 소스)
    handler = nts_handler({"1234567890": continuing("1234567890"), "1018112345": closed("1018112345", "20260930")})
    runner, s = api_runner(store, settings, handler)
    for _ in range(3):
        assert process_one(store, runner, s) == "FAILED"

    # ① 공공데이터포털 키가 없다 → 호출 없이 REVIEW/API_KEY_REQUIRED (관리자 큐 API_KEY, provider data_go_kr)
    assert process_one(store, runner, s) == "FAILED"
    assert handler.calls == []
    src = q(store, "SELECT s.status, s.status_reason, s.kind, t.risk_flags FROM source s JOIN site t ON t.id = s.site_id WHERE s.id = %s",
            check_id)
    assert src["status"] == "REVIEW" and src["status_reason"] == "API_KEY_REQUIRED" and src["kind"] == "CHECK"
    assert src["risk_flags"] == {"api_provider": "data_go_kr"}
    err = q(store, "SELECT code, message FROM crawl_error WHERE job_id = (SELECT max(id) FROM crawl_job WHERE source_id = %s)", check_id)
    assert err["code"] == "API_KEY_REQUIRED" and "data.go.kr" in err["message"]
    assert catalog.get("data_go_kr").key_required_message == err["message"]

    # ② 관리자가 서비스키를 넣는다 → 검증 → 설정 v1 그대로 ACTIVE(1440분) → 첫 점검: 가전자 폐업 HIGH 1건
    put_credential(store, "data_go_kr", GOOD_KEY)
    store.conn.execute("INSERT INTO crawl_job (source_id, job_type, trigger) VALUES (%s, 'ONBOARD', 'MANUAL')", (check_id,))
    assert process_one(store, runner, s) == "SUCCESS"
    src = q(store, "SELECT s.status, s.status_reason, s.interval_minutes, v.version, v.config FROM source s "
                   "JOIN source_config_version v ON v.id = s.active_config_version_id WHERE s.id = %s", check_id)
    assert src["status"] == "ACTIVE" and src["status_reason"] is None and src["interval_minutes"] == 1440 and src["version"] == 1
    assert src["config"] == {"method": "API", "provider": "data_go_kr", "check": "nts_status"}
    assert handler.calls[0][0] == ["0000000000"] and sorted(handler.calls[1][0]) == ["1018112345", "1234567890"]   # 번호 없는 회사는 안 보냄
    job = q(store, "SELECT status, found_count, new_count FROM crawl_job WHERE source_id = %s ORDER BY id DESC LIMIT 1", check_id)
    assert (job["status"], job["found_count"], job["new_count"]) == ("SUCCESS", 2, 1)        # 확인 2곳, 새 신호 1건
    rows = signals(store)
    assert len(rows) == 1 and rows[0]["company_id"] == ga and rows[0]["kind"] == "BIZ_STATUS" and rows[0]["severity"] == "HIGH"
    assert rows[0]["title"] == "폐업자 (폐업일 2026-09-30)" and rows[0]["fingerprint"] == "0320260930" and rows[0]["source_id"] == check_id
    assert rows[0]["detail"]["b_stt_cd"] == "03" and rows[0]["detail"]["end_dt"] == "20260930"
    last = q(store, "SELECT last_biz_status FROM company WHERE id = %s", ga)["last_biz_status"]
    assert last["b_stt"] == "폐업자" and last["tax_type"] == "부가가치세 일반과세자" and "checked_at" in last
    assert q(store, "SELECT last_biz_status->>'b_stt' AS s FROM company WHERE id = %s", nabu)["s"] == "계속사업자"
    assert q(store, "SELECT last_biz_status FROM company WHERE id = %s", nobiz)["last_biz_status"] is None
    assert q(store, "SELECT status FROM api_credential WHERE provider = 'data_go_kr'")["status"] == "VALID"
    blob = " ".join((r["message"] or "") + (r["url"] or "") for r in store.conn.execute("SELECT message, url FROM crawl_error"))
    assert GOOD_KEY not in blob

    # ③ 다음 날 같은 상태 → fingerprint가 같아 신호가 늘지 않는다 (found 2, new 0). 급감 감지도 없다
    assert crawl(store, runner, s, check_id) == "SUCCESS"
    job = q(store, "SELECT found_count, new_count FROM crawl_job WHERE source_id = %s ORDER BY id DESC LIMIT 1", check_id)
    assert (job["found_count"], job["new_count"]) == (2, 0) and len(signals(store)) == 1
    assert q(store, "SELECT consecutive_anomalies FROM source WHERE id = %s", check_id)["consecutive_anomalies"] == 0

    # ④ 나부품이 휴업으로 바뀌면 HIGH 신호 1건 추가, 가전자는 그대로
    handler2 = nts_handler({"1234567890": suspended("1234567890"), "1018112345": closed("1018112345", "20260930")})
    runner2, s2 = api_runner(store, settings, handler2)
    assert crawl(store, runner2, s2, check_id) == "SUCCESS"
    rows = signals(store)
    assert [(r["company_id"], r["severity"], r["fingerprint"]) for r in rows] == [(ga, "HIGH", "0320260930"), (nabu, "HIGH", "02")]
    assert q(store, "SELECT new_count FROM crawl_job WHERE source_id = %s ORDER BY id DESC LIMIT 1", check_id)["new_count"] == 1

    # ⑤ 키가 폐기됐다 → REVIEW/API_KEY_INVALID, 자격 증명 INVALID, 키는 어디에도 없다
    put_credential(store, "data_go_kr", "revoked-key")
    assert crawl(store, runner2, s2, check_id) == "FAILED"
    src = q(store, "SELECT status, status_reason FROM source WHERE id = %s", check_id)
    assert src["status"] == "REVIEW" and src["status_reason"] == "API_KEY_INVALID"
    cred = q(store, "SELECT status, last_error FROM api_credential WHERE provider = 'data_go_kr'")
    assert cred["status"] == "INVALID" and "revoked-key" not in cred["last_error"]
    err = q(store, "SELECT code, url FROM crawl_error ORDER BY id DESC LIMIT 1")
    assert err["code"] == "API_KEY_INVALID" and "serviceKey=***" in err["url"]


# ---------------------------------------------------------------- 회사 점검 (M3-2): DART 고유번호 매핑 → 회사별 공시 신호, 조달청 부정당업자 신호

def test_dart_checks_fill_corp_codes_and_record_disclosure_signals_once(env):
    from datetime import datetime, timedelta, timezone
    from fakedart import GOOD_KEY, dart_handler, disclosure
    store, _, settings, _ = env
    nabu, _ = register_company(store, "나부품", ["NABU"], "1234567890")       # corp_code 없음 → 매핑으로 채운다
    ga, _ = register_company(store, "가전자", [], None)
    store.conn.execute("UPDATE company SET corp_code = '00000002' WHERE id = %s", (ga,))   # 손으로 넣은 값은 그대로 둔다
    dup, _ = register_company(store, "다중복", [], None)                      # DART에 같은 이름의 비상장사 둘 → 채우지 않는다
    map_id = register_check_source(store, "dart_corp_map")
    disc_id = register_check_source(store, "dart_disclosure")
    rows = [disclosure("00000001", "나부품", "회생절차 개시신청", "20261016000001"),
            disclosure("00000001", "나부품", "사업보고서 (2025.12)", "20261016000002", days_ago=1),
            disclosure("00000002", "가전자", "주요사항보고서(유상증자결정)", "20261016000003", stock_code="001234"),
            disclosure("00126380", "테스트전자", "회생절차 개시신청", "20261016000009")]      # 마스터에 없는 회사: 받지 않는다
    handler = dart_handler(rows)
    runner, s = api_runner(store, settings, handler)
    put_credential(store, "dart", GOOD_KEY)
    for _ in range(3):
        assert process_one(store, runner, s) == "FAILED"     # 회사 감시 세 건은 네이버 키가 없어 REVIEW

    # ① 고유번호 매핑 (주 1회): 나부품만 채워진다. 가전자는 이미 있고 다중복은 모호
    assert process_one(store, runner, s) == "SUCCESS"
    src = q(store, "SELECT s.status, s.interval_minutes, t.domain, v.config FROM source s JOIN site t ON t.id = s.site_id "
                   "JOIN source_config_version v ON v.id = s.active_config_version_id WHERE s.id = %s", map_id)
    assert src["status"] == "ACTIVE" and src["interval_minutes"] == 10080 and src["domain"] == "opendart.fss.or.kr"
    assert src["config"] == {"method": "API", "provider": "dart", "check": "dart_corp_map"}
    job = q(store, "SELECT status, found_count, new_count FROM crawl_job WHERE source_id = %s ORDER BY id DESC LIMIT 1", map_id)
    assert (job["status"], job["found_count"], job["new_count"]) == ("SUCCESS", 2, 1)
    codes = {r["name"]: r["corp_code"] for r in store.conn.execute("SELECT name, corp_code FROM company")}
    assert codes == {"나부품": "00000001", "가전자": "00000002", "다중복": None}
    assert sum("corpCode.xml" in u for u in handler.calls) == 1
    # 점검 소스는 백엔드가 registration_request 없이 만든다(시스템 소스): 진행 메시지는 소스 상태·작업 기록으로 본다
    assert q(store, "SELECT status FROM api_credential WHERE provider = 'dart'")["status"] == "VALID"

    # ② 회사별 공시 (6시간): corp_code가 있는 회사마다 받아 문서 3건(API 연결), 신호 2건 (회생 HIGH, 유상증자 MEDIUM). 사업보고서는 문서만
    assert process_one(store, runner, s) == "SUCCESS"
    src = q(store, "SELECT status, interval_minutes FROM source WHERE id = %s", disc_id)
    assert src["status"] == "ACTIVE" and src["interval_minutes"] == 360
    job = q(store, "SELECT found_count, new_count FROM crawl_job WHERE source_id = %s ORDER BY id DESC LIMIT 1", disc_id)
    assert (job["found_count"], job["new_count"]) == (2, 2)
    docs = store.conn.execute("SELECT d.title, d.source_id, d.url, dc.company_id, dc.matched_by FROM document d "
                              "JOIN document_company dc ON dc.document_id = d.id ORDER BY d.id").fetchall()
    assert [(d["title"], d["company_id"], d["matched_by"]) for d in docs] == [
        ("회생절차 개시신청", nabu, "API"), ("사업보고서 (2025.12)", nabu, "API"), ("주요사항보고서(유상증자결정)", ga, "API")]
    assert all(d["source_id"] == disc_id and d["url"].startswith("https://dart.fss.or.kr/dsaf001/main.do?rcpNo=") for d in docs)
    assert q(store, "SELECT count(*) AS c FROM document")["c"] == 3 and "corp_code=00126380" not in " ".join(handler.calls)
    rows_ = signals(store)
    assert [(r["company_id"], r["kind"], r["severity"], r["fingerprint"]) for r in rows_] == [
        (nabu, "DISCLOSURE", "HIGH", "20261016000001"), (ga, "DISCLOSURE", "MEDIUM", "20261016000003")]
    assert rows_[0]["title"] == "회생절차 개시신청" and rows_[0]["source_id"] == disc_id
    assert rows_[0]["document_id"] == q(store, "SELECT id FROM document WHERE url LIKE '%%20261016000001'")["id"]
    assert rows_[0]["detail"]["matched"] == ["회생절차"] and rows_[0]["detail"]["rcept_no"] == "20261016000001"
    today_kst = datetime.now(timezone(timedelta(hours=9))).date()
    assert rows_[0]["observed_at"].astimezone(timezone(timedelta(hours=9))).date() == today_kst   # observed_at = 접수일(KST)

    # ③ 다음 주기: 같은 공시는 문서도 신호도 늘지 않는다
    assert crawl(store, runner, s, disc_id) == "SUCCESS"
    job = q(store, "SELECT found_count, new_count FROM crawl_job WHERE source_id = %s ORDER BY id DESC LIMIT 1", disc_id)
    assert (job["found_count"], job["new_count"]) == (2, 0) and len(signals(store)) == 2
    assert q(store, "SELECT count(*) AS c FROM document")["c"] == 3 and q(store, "SELECT count(*) AS c FROM document_company")["c"] == 3
    assert q(store, "SELECT max(version) AS v FROM document")["v"] == 1

    # ④ 매핑을 다시 돌리면 대상은 다중복 하나, 채운 것 없음
    assert crawl(store, runner, s, map_id) == "SUCCESS"
    job = q(store, "SELECT found_count, new_count FROM crawl_job WHERE source_id = %s ORDER BY id DESC LIMIT 1", map_id)
    assert (job["found_count"], job["new_count"]) == (1, 0)
    assert q(store, "SELECT corp_code FROM company WHERE id = %s", dup)["corp_code"] is None
    blob = " ".join((r["message"] or "") + (r["url"] or "") for r in store.conn.execute("SELECT message, url FROM crawl_error"))
    assert GOOD_KEY not in blob


def test_g2b_check_records_debarment_signal_once_and_skips_companies_without_biz_no(env):
    from datetime import datetime, timedelta, timezone
    from fakeg2b import GOOD_KEY, g2b_handler, sanction
    store, _, settings, _ = env
    nabu, _ = register_company(store, "나부품", [], "1234567890")
    ga, _ = register_company(store, "가전자", [], None)
    check_id = register_check_source(store, "g2b_debarment")
    handler = g2b_handler({"1234567890": [sanction("1234567890", "나부품")]})
    runner, s = api_runner(store, settings, handler)
    put_credential(store, "data_go_kr", GOOD_KEY)
    for _ in range(2):
        assert process_one(store, runner, s) == "FAILED"     # 회사 감시 두 건: 네이버 키 없음

    # ① 첫 점검: 시험 호출 + 번호 있는 회사만 조회 → 나부품 DEBARMENT HIGH 1건
    assert process_one(store, runner, s) == "SUCCESS"
    src = q(store, "SELECT s.status, s.interval_minutes, t.domain, t.risk_flags, v.config FROM source s JOIN site t ON t.id = s.site_id "
                   "JOIN source_config_version v ON v.id = s.active_config_version_id WHERE s.id = %s", check_id)
    assert src["status"] == "ACTIVE" and src["interval_minutes"] == 1440 and src["domain"] == "apis.data.go.kr"
    assert src["risk_flags"] == {"api_provider": "data_go_kr"}
    assert src["config"] == {"method": "API", "provider": "data_go_kr", "check": "g2b_debarment"}
    assert handler.calls == [("1", "0000000000", "1"), ("1", "1234567890", "1")]
    job = q(store, "SELECT status, found_count, new_count FROM crawl_job WHERE source_id = %s ORDER BY id DESC LIMIT 1", check_id)
    assert (job["status"], job["found_count"], job["new_count"]) == ("SUCCESS", 2, 1)
    rows = signals(store)
    assert len(rows) == 1
    assert (rows[0]["company_id"], rows[0]["kind"], rows[0]["severity"], rows[0]["fingerprint"]) == (nabu, "DEBARMENT", "HIGH", "20260101:조달청")
    assert rows[0]["title"] == "입찰참가자격 제한 (2026-01-01~2026-06-30, 조달청)" and rows[0]["source_id"] == check_id
    assert rows[0]["detail"]["raw"]["bizno"] == "1234567890" and rows[0]["detail"]["agency"] == "조달청"
    assert rows[0]["observed_at"] == datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=9)))
    assert q(store, "SELECT status FROM api_credential WHERE provider = 'data_go_kr'")["status"] == "VALID"

    # ② 다음 날 같은 제재 → 신호가 늘지 않는다
    assert crawl(store, runner, s, check_id) == "SUCCESS"
    job = q(store, "SELECT found_count, new_count FROM crawl_job WHERE source_id = %s ORDER BY id DESC LIMIT 1", check_id)
    assert (job["found_count"], job["new_count"]) == (2, 0) and len(signals(store)) == 1
    blob = " ".join((r["message"] or "") + (r["url"] or "") for r in store.conn.execute("SELECT message, url FROM crawl_error"))
    assert GOOD_KEY not in blob

    # ③ 키가 폐기됐다 (게이트웨이 30) → REVIEW/API_KEY_INVALID, 자격 증명 INVALID, 키는 어디에도 없다
    put_credential(store, "data_go_kr", "revoked-key")
    assert crawl(store, runner, s, check_id) == "FAILED"
    src = q(store, "SELECT status, status_reason FROM source WHERE id = %s", check_id)
    assert src["status"] == "REVIEW" and src["status_reason"] == "API_KEY_INVALID"
    cred = q(store, "SELECT status, last_error FROM api_credential WHERE provider = 'data_go_kr'")
    assert cred["status"] == "INVALID" and "revoked-key" not in cred["last_error"]
    err = q(store, "SELECT code, url FROM crawl_error ORDER BY id DESC LIMIT 1")
    assert err["code"] == "API_KEY_INVALID" and "ServiceKey=***" in err["url"]


def test_claim_job_alternates_onboard_and_crawl_three_to_one(env):
    # 일괄 등록 6건(ONBOARD)과 주기 수집 3건(CRAWL)이 함께 대기 중이면 ONBOARD 셋마다 CRAWL 하나를 집는다
    store, runner, settings, sites = env
    waiting = [register(store, f"https://onboard{i}.example") for i in range(6)]
    for i in range(3):
        sid = register(store, f"https://due{i}.example")
        store.conn.execute("UPDATE crawl_job SET status = 'SUCCESS' WHERE source_id = %s", (sid,))
        store.conn.execute("INSERT INTO crawl_job (source_id, job_type, trigger) VALUES (%s, 'CRAWL', 'SCHEDULE')", (sid,))

    order = []
    while (job := store.claim_job("w1", 30)) is not None:
        order.append(job.job_type[0])
    assert "".join(order) == "OOOCOOOCC"
    assert store.claimed == 9
    # 같은 유형 안에서는 오래 기다린 순
    rows = store.conn.execute("SELECT source_id FROM crawl_job WHERE job_type = 'ONBOARD' AND status = 'RUNNING' "
                              "ORDER BY started_at, id").fetchall()
    assert [r["source_id"] for r in rows] == waiting

    # 비율 0이면 예전 규칙: ONBOARD만 먼저 다 집는다
    store.conn.execute("UPDATE crawl_job SET status = 'QUEUED', locked_by = NULL, started_at = NULL WHERE status = 'RUNNING'")
    legacy = Store(DSN, onboard_ratio=0)
    try:
        order = []
        while (job := legacy.claim_job("w2", 30)) is not None:
            order.append(job.job_type[0])
        assert "".join(order) == "OOOOOOCCC"
    finally:
        legacy.close()


def test_adaptive_interval_doubles_for_quiet_site_and_respects_lock(env):
    store, runner, settings, sites = env
    site = Site(news_site_routes()); sites.append(site)
    source_id = register(store, site.base)
    assert process_one(store, runner, settings) == "SUCCESS"                   # 온보딩: 60분 (NEWS 기본값)

    # 새 기사가 없는 주기 수집 9회까지는 지켜본다 (ONBOARD의 첫 수집은 세지 않는다)
    for _ in range(9):
        assert crawl(store, runner, settings, source_id) == "SUCCESS"
    src = q(store, "SELECT interval_minutes FROM source WHERE id = %s", source_id)
    assert src["interval_minutes"] == 60
    assert q(store, "SELECT count(*) AS c FROM crawl_job WHERE note IS NOT NULL")["c"] == 0

    # 10회째: 최근 10회 평균 0건 → 60→120분, 실행 메모와 next_run_at에 반영
    assert crawl(store, runner, settings, source_id) == "SUCCESS"
    src = q(store, "SELECT interval_minutes, next_run_at FROM source WHERE id = %s", source_id)
    assert src["interval_minutes"] == 120
    job = q(store, "SELECT note, status FROM crawl_job ORDER BY id DESC LIMIT 1")
    assert job["status"] == "SUCCESS" and job["note"].startswith("주기 자동 조정 60→120분")
    assert q(store, "SELECT (next_run_at - now()) > interval '100 minutes' AS later FROM source WHERE id = %s", source_id)["later"]

    # 계속 조용하면 다시 두 배
    assert crawl(store, runner, settings, source_id) == "SUCCESS"
    assert q(store, "SELECT interval_minutes FROM source WHERE id = %s", source_id)["interval_minutes"] == 240

    # 사람이 소스 상세에서 주기를 정하면(interval_locked) 자동 조정이 덮지 않는다
    store.conn.execute("UPDATE source SET interval_minutes = 30, interval_locked = TRUE WHERE id = %s", (source_id,))
    assert crawl(store, runner, settings, source_id) == "SUCCESS"
    src = q(store, "SELECT interval_minutes, interval_locked FROM source WHERE id = %s", source_id)
    assert src["interval_minutes"] == 30 and src["interval_locked"]
    assert q(store, "SELECT note FROM crawl_job ORDER BY id DESC LIMIT 1")["note"] is None

    # 재탐색(ONBOARD 다시)도 잠긴 주기를 기본값으로 되돌리지 않는다
    store.conn.execute("INSERT INTO crawl_job (source_id, job_type, trigger) VALUES (%s, 'ONBOARD', 'MANUAL')", (source_id,))
    assert process_one(store, runner, settings) == "SUCCESS"
    assert q(store, "SELECT interval_minutes FROM source WHERE id = %s", source_id)["interval_minutes"] == 30

    # 설정으로 끌 수 있다
    store.conn.execute("UPDATE source SET interval_minutes = 60, interval_locked = FALSE WHERE id = %s", (source_id,))
    off = Settings(db_url=DSN, min_interval_seconds=0, max_retries=1, allow_private_hosts=True, worker_id="test-worker",
                   evidence_dir=settings.evidence_dir, adaptive_interval=False)
    quiet_runner = JobRunner(store, runner.f, off)
    assert crawl(store, quiet_runner, off, source_id) == "SUCCESS"
    assert q(store, "SELECT interval_minutes FROM source WHERE id = %s", source_id)["interval_minutes"] == 60


# ---------------------------------------------------------------------- 링크 모음(포털) 페이지 → 언론사 확장

def portal_site() -> Site:
    """뉴스스탠드처럼 다른 호스트의 언론사 로고만 모아 둔 홈. 같은 사이트 기사가 없어 자동 탐색은 실패한다."""
    logos = "".join(f'<li><a href="https://www.press{i:02d}.co.kr/"><img alt="프레스{i:02d}일보"></a></li>' for i in range(12))
    return Site({"/": (200, "text/html; charset=utf-8", f"<html><body><ul>{logos}</ul></body></html>"),
                 "/robots.txt": (404, "text/plain", "")})


OUTLETS = [("a-ilbo.example.com", "https://www.a-ilbo.example.com", "A일보"),
           ("b-ilbo.example.com", "https://b-ilbo.example.com", "B일보"),
           ("c-news.example.com", "https://c-news.example.com", "C뉴스")]


def stub_portal(monkeypatch, outlets) -> dict:
    """jobs가 portal.detect로 부르므로 모듈 속성을 바꾸면 된다. 무엇을 받았는지 돌려준다."""
    from sensing_worker import portal
    seen: dict = {}

    def fake_detect(html, url, domain):
        seen.update(html=html, url=url, domain=domain)
        return [portal.Outlet(*o) for o in outlets]

    monkeypatch.setattr(portal, "detect", fake_detect)
    return seen


def test_portal_page_registers_the_outlets_it_links_to(env, monkeypatch):
    store, runner, settings, sites = env
    site = portal_site(); sites.append(site)
    source_id = register(store, site.base)
    # B일보는 이미 열린 소스가 있다 → 건너뛴다
    store.conn.execute("INSERT INTO site (domain, base_url) VALUES ('b-ilbo.example.com', 'https://b-ilbo.example.com')")
    store.conn.execute("INSERT INTO source (site_id, name, status, requested_by) "
                       "SELECT id, domain, 'ACTIVE', 1 FROM site WHERE domain = 'b-ilbo.example.com'")
    seen = stub_portal(monkeypatch, OUTLETS)

    assert process_one(store, runner, settings) == "SUCCESS"

    # 사전 점검에서 받은 홈으로 판정했다 (다시 받지 않는다)
    assert "프레스00일보" in seen["html"] and seen["domain"] == "127.0.0.1" and site.hits.count("/") == 2  # 점검 1 + 탐색 1
    src = q(store, "SELECT status, status_reason FROM source WHERE id = %s", source_id)
    assert src["status"] == "EXCLUDED" and src["status_reason"] == "PORTAL_EXPANDED"
    reg = q(store, "SELECT status, step, result_message, batch_id FROM registration_request WHERE source_id = %s", source_id)
    assert reg["status"] == "COMPLETED" and reg["step"] == "DONE" and reg["batch_id"] is None
    assert reg["result_message"] == ("링크 모음 페이지입니다. 이 페이지가 가리키는 언론사 2곳을 대신 등록했습니다. "
                                     f"(이미 등록된 1곳 제외) [묶음 portal:{source_id}]")
    job = q(store, "SELECT status, found_count, new_count, error_count FROM crawl_job WHERE source_id = %s", source_id)
    assert job["status"] == "SUCCESS" and job["found_count"] == 3 and job["new_count"] == 2 and job["error_count"] == 0
    assert q(store, "SELECT count(*) AS c FROM crawl_error")["c"] == 0

    # 언론사 2곳: 백엔드 등록과 같은 행 (사이트·소스 PENDING·등록 요청 PROCESSING/QUEUED·ONBOARD 작업), 같은 요청자·채널
    rows = store.conn.execute("""
        SELECT r.normalized_domain, r.input_url, r.user_id, r.channel, r.status, r.step, r.result_message,
               s.status AS src_status, s.kind, s.requested_by, s.name AS src_name, t.base_url, t.name AS site_name,
               j.job_type, j.trigger, j.status AS job_status
        FROM registration_request r JOIN source s ON s.id = r.source_id JOIN site t ON t.id = s.site_id
        JOIN crawl_job j ON j.source_id = s.id WHERE r.batch_id = %s ORDER BY r.id""", (f"portal:{source_id}",)).fetchall()
    assert [r["normalized_domain"] for r in rows] == ["a-ilbo.example.com", "c-news.example.com"]
    a = rows[0]
    assert a["input_url"] == "https://www.a-ilbo.example.com" and a["base_url"] == "https://www.a-ilbo.example.com"
    assert a["user_id"] == 2 and a["channel"] == "WEB" and a["status"] == "PROCESSING" and a["step"] == "QUEUED"
    assert "접수" in a["result_message"]
    assert a["src_status"] == "PENDING" and a["kind"] == "SITE" and a["requested_by"] == 2
    assert a["src_name"] == "a-ilbo.example.com" and a["site_name"] == "A일보"
    assert a["job_type"] == "ONBOARD" and a["trigger"] == "REGISTRATION" and a["job_status"] == "QUEUED"
    # 이미 있던 B일보는 사이트·소스·요청이 늘지 않았다
    assert q(store, "SELECT count(*) AS c FROM source s JOIN site t ON t.id = s.site_id WHERE t.domain = 'b-ilbo.example.com'")["c"] == 1
    assert q(store, "SELECT count(*) AS c FROM registration_request WHERE normalized_domain = 'b-ilbo.example.com'")["c"] == 0
    assert q(store, "SELECT count(*) AS c FROM audit_log WHERE action = 'SOURCE_REGISTERED'")["c"] == 2
    # 다음 작업은 언론사들의 ONBOARD다 (큐에 올라가 있다)
    assert q(store, "SELECT count(*) AS c FROM crawl_job WHERE status = 'QUEUED' AND job_type = 'ONBOARD'")["c"] == 2


def test_portal_expansion_respects_outlet_cap(env, monkeypatch):
    store, runner, settings, sites = env
    site = portal_site(); sites.append(site)
    source_id = register(store, site.base)
    stub_portal(monkeypatch, OUTLETS)
    capped = Settings(**{**settings.__dict__, "portal_max_outlets": 1})
    assert process_one(store, runner.__class__(store, runner.f, capped), capped) == "SUCCESS"
    reg = q(store, "SELECT result_message FROM registration_request WHERE source_id = %s", source_id)
    assert "언론사 1곳을" in reg["result_message"] and "상한을 넘은 2곳 제외" in reg["result_message"]
    assert q(store, "SELECT count(*) AS c FROM registration_request WHERE batch_id = %s", f"portal:{source_id}")["c"] == 1


def test_non_portal_discovery_failure_still_goes_to_review(env, monkeypatch):
    """모음 페이지가 아니면(언론사 후보 없음) 예전처럼 REVIEW/DISCOVERY_FAILED."""
    store, runner, settings, sites = env
    site = portal_site(); sites.append(site)
    source_id = register(store, site.base)
    stub_portal(monkeypatch, [])
    assert process_one(store, runner, settings) == "FAILED"
    src = q(store, "SELECT status, status_reason FROM source WHERE id = %s", source_id)
    assert src["status"] == "REVIEW" and src["status_reason"] == "DISCOVERY_FAILED"
    assert q(store, "SELECT status FROM registration_request")["status"] == "REVIEW"


# ---------------------------------------------------------------------- 색인·이슈 묶기 (M5 기반)

def site_source(store: Store, domain: str, site_type: str) -> int:
    c = store.conn
    site_id = c.execute("INSERT INTO site (domain, base_url, site_type) VALUES (%s, %s, %s) RETURNING id",
                        (domain, f"https://{domain}", site_type)).fetchone()["id"]
    return c.execute("INSERT INTO source (site_id, name, status) VALUES (%s, %s, 'ACTIVE') RETURNING id",
                     (site_id, domain)).fetchone()["id"]


def add_doc(store: Store, source_id: int, url: str, title: str, hours_ago: float, body: str | None = None) -> int:
    from datetime import datetime, timedelta, timezone
    from sensing_worker.db import sha256
    published = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return store.conn.execute(
        """
        INSERT INTO document (source_id, url, url_hash, title, published_at, body_text, content_hash)
        VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
        """, (source_id, url, sha256(url), title, published, body, sha256(title + "\n" + (body or "")))).fetchone()["id"]


def issue_of(store: Store, document_id: int) -> dict | None:
    return q(store, "SELECT i.* FROM issue i JOIN issue_document d ON d.issue_id = i.id WHERE d.document_id = %s",
             document_id)


def test_index_pending_builds_chunks_and_groups_issues(env):
    from sensing_worker.indexer import index_pending
    store, runner, settings, sites = env
    a, b, c = (site_source(store, f"press{i}.example.com", "NEWS") for i in "abc")
    gov = site_source(store, "gov.example.go.kr", "PUBLIC")
    yasu = [
        add_doc(store, a, "https://press-a/1", "다카이치 총리, 야스쿠니 신사에 공물 봉납", 5),
        add_doc(store, b, "https://press-b/1", "日 다카이치, 야스쿠니 공물 봉납…외교부 유감 표명", 4),
        add_doc(store, c, "https://press-c/1", "정부, 다카이치 야스쿠니 공물 봉납에 깊은 유감", 3),
        add_doc(store, a, "https://press-a/2", "다카이치 총리, 야스쿠니 신사에 공물 봉납(종합)", 2),   # 같은 언론사 중복
    ]
    apt = add_doc(store, b, "https://press-b/2", "서울 아파트값 88주 연속 상승", 1)
    body = "\n\n".join(f"{i}번째 문단: 반도체 지원사업 신청 요건과 일정을 안내합니다. " * 8 for i in range(6))
    notice = add_doc(store, gov, "https://gov/1", "2026년 반도체 지원사업 공고", 6, body=body)
    press_with_body = add_doc(store, c, "https://press-c/2", "할리스 고객 개인정보 유출", 1, body="언론 기사 본문은 색인하지 않는다")
    old = add_doc(store, c, "https://press-c/old", "다카이치 총리, 야스쿠니 신사에 공물 봉납", 24 * 10)   # 창 밖의 예전 기사

    out = index_pending(store, settings, limit=100)
    assert out.fetched == 8
    assert q(store, "SELECT count(*) AS c FROM document WHERE indexed_at IS NULL")["c"] == 0

    # 조각: 언론 기사는 제목 하나(seq 0), 공공 문서는 제목 + 본문 조각, NEWS 사이트는 본문이 있어도 제목만
    chunks = store.conn.execute("SELECT document_id, seq, text, index_version FROM chunk ORDER BY document_id, seq").fetchall()
    by_doc: dict[int, list[dict]] = {}
    for ch in chunks:
        by_doc.setdefault(ch["document_id"], []).append(ch)
    assert [ch["text"] for ch in by_doc[yasu[0]]] == ["다카이치 총리, 야스쿠니 신사에 공물 봉납"]
    assert [ch["text"] for ch in by_doc[press_with_body]] == ["할리스 고객 개인정보 유출"]
    assert by_doc[notice][0]["text"] == "2026년 반도체 지원사업 공고" and len(by_doc[notice]) >= 3
    assert all(len(ch["text"]) <= 800 for ch in by_doc[notice])
    assert {ch["index_version"] for ch in chunks} <= {"fts-kiwi-v1", "fts-regex-v1"}
    assert out.chunks == len(chunks)

    def fts(query: str) -> set[int]:
        rows = store.conn.execute("SELECT DISTINCT document_id FROM chunk WHERE tokens @@ to_tsquery('simple', %s)",
                                  (query,)).fetchall()
        return {r["document_id"] for r in rows}

    assert fts("반도체:* & 요건:*") == {notice}                  # 본문 조각까지 색인된다
    assert fts("봉납:*") == set(yasu) | {old}
    assert fts("야스쿠니:* & 외교부:*") == {yasu[1]}
    assert fts("본문:*") == set()                                # 언론 기사 본문은 색인하지 않는다

    # 이슈: 같은 사건 4건(언론사 3곳)이 한 이슈, 10일 전 같은 제목은 창 밖이라 다른 이슈이고 닫힌다
    issue = issue_of(store, yasu[0])
    assert all(issue_of(store, d)["id"] == issue["id"] for d in yasu)
    assert issue["doc_count"] == 4 and issue["source_count"] == 3 and issue["status"] == "OPEN"
    assert {"다카이치", "야스쿠니", "봉납"} <= set(issue["top_terms"])
    assert issue["title"] in {"다카이치 총리, 야스쿠니 신사에 공물 봉납", "다카이치 총리, 야스쿠니 신사에 공물 봉납(종합)",
                              "日 다카이치, 야스쿠니 공물 봉납…외교부 유감 표명", "정부, 다카이치 야스쿠니 공물 봉납에 깊은 유감"}
    first = q(store, "SELECT published_at FROM document WHERE id = %s", yasu[0])["published_at"]
    last = q(store, "SELECT published_at FROM document WHERE id = %s", yasu[3])["published_at"]
    assert issue["first_seen_at"] == first and issue["last_seen_at"] == last
    old_issue = issue_of(store, old)
    assert old_issue["id"] != issue["id"] and old_issue["status"] == "CLOSED" and out.issues.closed >= 1
    assert issue_of(store, apt)["id"] not in (issue["id"], old_issue["id"]) and issue_of(store, apt)["doc_count"] == 1
    assert out.issues.created + out.issues.joined + out.issues.skipped == 8 and out.issues.joined == 3

    assert index_pending(store, settings, limit=100).fetched == 0   # 더 할 것이 없다


def test_reindex_after_document_change_replaces_chunks_and_moves_issue(env):
    from sensing_worker.indexer import index_pending
    store, runner, settings, sites = env
    a, b, c = (site_source(store, f"press{i}.example.com", "NEWS") for i in "abc")
    gov = site_source(store, "gov.example.go.kr", "PUBLIC")
    docs = [
        add_doc(store, a, "https://press-a/1", "할리스 멤버십 해킹, 고객 개인정보 유출", 3),
        add_doc(store, b, "https://press-b/1", "할리스 고객 개인정보 유출…비밀번호·주소 포함", 2),
        add_doc(store, c, "https://press-c/1", "할리스 해킹 개인정보 유출, 비밀번호 바꿔야", 1),
    ]
    notice = add_doc(store, gov, "https://gov/1", "청년 창업 지원사업 공고", 5, body="첫 본문 문단입니다. " * 10)
    assert index_pending(store, settings, limit=100).fetched == 4
    issue = issue_of(store, docs[0])
    assert issue["doc_count"] == 3 and issue["source_count"] == 3
    before = {r["id"] for r in store.conn.execute("SELECT id FROM chunk WHERE document_id = %s", (notice,)).fetchall()}

    # 본문이 바뀌면 upsert_document가 version을 올리고 indexed_at을 비운다 → 다시 색인하며 조각을 갈아 끼운다
    _, status = store.upsert_document(source_id=gov, job_id=None, url="https://gov/1", title="청년 창업 지원사업 공고",
                                      author=None, section=None, published_at=None,
                                      body="바뀐 본문: 신청 기한이 연장되었습니다. " * 60)
    assert status == "UPDATED"
    # 기사 제목이 다른 사건으로 바뀐 경우: 원래 이슈에서 빠지고 남은 이슈의 통계가 다시 계산된다
    store.upsert_document(source_id=c, job_id=None, url="https://press-c/1", title="서울 지하철 노조 파업 예고",
                          author=None, section=None, published_at=None, body=None)
    out = index_pending(store, settings, limit=100)
    assert out.fetched == 2
    after = store.conn.execute("SELECT id, seq, text FROM chunk WHERE document_id = %s ORDER BY seq", (notice,)).fetchall()
    assert before.isdisjoint({r["id"] for r in after}) and len(after) >= 3
    assert "연장" in after[1]["text"] and not any("첫 본문" in r["text"] for r in after)
    assert q(store, "SELECT count(*) AS c FROM chunk WHERE tokens @@ to_tsquery('simple', '연장:*')")["c"] >= 1
    assert q(store, "SELECT count(*) AS c FROM chunk WHERE tokens @@ to_tsquery('simple', '문단:*')")["c"] == 0

    moved = issue_of(store, docs[2])
    kept = issue_of(store, docs[0])
    assert moved["id"] != kept["id"] and moved["doc_count"] == 1
    assert kept["doc_count"] == 2 and kept["source_count"] == 2
    assert q(store, "SELECT count(*) AS c FROM issue_document")["c"] == 4


def test_crawled_articles_are_matched_against_keyword_watches(env):
    """네이버 키가 없어 REVIEW에 멈춘 감시라도, 우리가 긁어 온 기사에 키워드가 있으면 document_watch로 기록된다."""
    store, runner, settings, sites = env
    watch_id = register_watch(store, "자사 보도", ["기사 제목 103번", "없는말"], user_id=2)
    site = Site(news_site_routes(range(101, 106))); sites.append(site)
    source_id = register(store, site.base)
    assert process_one(store, runner, settings) in ("SUCCESS", "FAILED")   # 감시 ONBOARD(키 없음 → REVIEW) 또는 사이트
    assert process_one(store, runner, settings) in ("SUCCESS", "FAILED")
    hits = store.conn.execute(
        "SELECT w.keyword, d.title FROM document_watch w JOIN document d ON d.id = w.document_id WHERE w.source_id = %s",
        (watch_id,)).fetchall()
    assert [(h["keyword"], h["title"]) for h in hits] == [("기사 제목 103번", "기사 제목 103번 소식")]
    assert q(store, "SELECT status FROM source WHERE id = %s", source_id)["status"] == "ACTIVE"
