"""PostgreSQL을 쓰는 통합 테스트: 백엔드가 만든 등록 작업을 워커가 끝까지 처리하는지 본다.

빈 테스트용 DB를 가리키는 SENSING_TEST_DSN이 있을 때만 실행한다. 스키마는 백엔드의 Flyway 파일을 그대로 쓴다.
    SENSING_TEST_DSN=postgresql://sensing:sensing@localhost:5432/sensing_test pytest
"""

from __future__ import annotations

import os
import pathlib

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
