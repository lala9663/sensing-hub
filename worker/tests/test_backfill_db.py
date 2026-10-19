"""과거 기사 채우기(BACKFILL)를 PostgreSQL과 가짜 사이트로 끝까지 돌린다.

보관 사이트맵(gzip) 고르기, 목록 페이지 역순 멈춤(날짜 있는 목록·없는 목록), 끊어 이어 하기, 양보, 취소, 중복 건너뛰기, 차단,
옛 기사가 지금 이슈에 붙지 않는지. SENSING_TEST_DSN이 있을 때만 실행한다 (test_flow_db의 env 픽스처를 쓴다).
"""

from __future__ import annotations

import gzip
import json
import os
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from fakesite import BODY, Site
from sensing_worker import indexer
from sensing_worker.discovery import pattern_from_signature
from sensing_worker.jobs import JobRunner
from sensing_worker.main import process_one
from test_flow_db import env, q  # noqa: F401  (env는 픽스처)

pytestmark = pytest.mark.skipif(not os.getenv("SENSING_TEST_DSN"), reason="SENSING_TEST_DSN not set")

KST = timezone(timedelta(hours=9))
NEWS_SIG = "/news/articleView.html?idxno={n}"


# ---------------------------------------------------------------------- 가짜 사이트·소스

def kst(d: date, hour: int = 10) -> datetime:
    return datetime(d.year, d.month, d.day, hour, tzinfo=KST)


def news_article(i: int, published: datetime, title: str | None = None) -> tuple[int, str, str]:
    return (200, "text/html; charset=utf-8", f"""<html><head><title>{title or f'과거 기사 {i}번'} - 아카이브 뉴스</title>
        <meta property="og:type" content="article"><meta property="og:title" content="{title or f'과거 기사 {i}번'}">
        <meta property="article:published_time" content="{published.isoformat()}"></head>
        <body><article id="article-view-content-div"><p>{BODY}</p></article></body></html>""")


# 기사 번호 → 발행일. 6월(기간 앞)·7~8월(기간)·9월(기간 뒤)
ARCHIVE = {**{200 + i: date(2026, 6, 10 + i) for i in range(1, 4)},
           **{300 + i: date(2026, 7, 2 + 3 * i) for i in range(0, 8)},
           **{400 + i: date(2026, 8, 3 + 4 * i) for i in range(0, 6)},
           **{500 + i: date(2026, 9, 5 + i) for i in range(1, 4)}}
PERIOD = (date(2026, 7, 1), date(2026, 8, 31))
IN_PERIOD = sorted(i for i, d in ARCHIVE.items() if PERIOD[0] <= d <= PERIOD[1])   # 300~307, 400~405
STALE = 399   # 사이트맵 lastmod는 기간 안(고친 날)인데 실제 발행은 6월


def archive_site(sites: list[Site]) -> Site:
    """언론사형 아카이브: robots.txt에 뉴스 사이트맵과 보관 사이트맵 색인. 보관 사이트맵은 두 달 단위 .xml.gz."""
    site = Site({}); sites.append(site)
    b = site.base

    def urlset(ids, lastmods=None) -> bytes:
        rows = "".join(f"<url><loc>{b}/news/articleView.html?idxno={i}</loc>"
                       f"<lastmod>{(lastmods or {}).get(i, kst(ARCHIVE.get(i, date(2026, 8, 30))).isoformat())}</lastmod></url>"
                       for i in ids)
        rows += f"<url><loc>{b}/news/articleList.html?sc_section_code=S1N1</loc></url>"   # 기사 아닌 주소
        return gzip.compress(f'<?xml version="1.0" encoding="UTF-8"?><urlset>{rows}</urlset>'.encode())

    index = ('<?xml version="1.0" encoding="UTF-8"?><sitemapindex>'
             + "".join(f"<sitemap><loc>{b}/sitemap/archive-{name}.xml.gz</loc><lastmod>{last}</lastmod></sitemap>"
                       for name, last in (("202605-202606", "2026-06-30"), ("202607-202608", "2026-08-31"),
                                          ("202609-202610", "2026-10-19")))
             + "</sitemapindex>")
    news = ('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">'
            + "".join(f"<url><loc>{b}/news/articleView.html?idxno={i}</loc><news:news><news:publication_date>"
                      f"{kst(ARCHIVE[i]).isoformat()}</news:publication_date></news:news></url>" for i in (501, 502, 503))
            + "</urlset>")
    site.routes.update({
        "/robots.txt": (200, "text/plain",
                        f"User-agent: *\nDisallow: /member/\nSitemap: {b}/sitemap/news.xml\nSitemap: {b}/sitemap/index.xml\n"),
        "/sitemap/news.xml": (200, "application/xml", news),
        "/sitemap/index.xml": (200, "application/xml", index),
        "/sitemap/archive-202605-202606.xml.gz": (200, "application/x-gzip", urlset(range(201, 204))),
        "/sitemap/archive-202607-202608.xml.gz": (200, "application/x-gzip",
                                                  urlset(list(range(300, 308)) + list(range(400, 406)) + [STALE])),
        "/sitemap/archive-202609-202610.xml.gz": (200, "application/x-gzip", urlset(range(501, 504))),
    })
    for i, d in ARCHIVE.items():
        site.routes[f"/news/articleView.html?idxno={i}"] = news_article(i, kst(d))
    site.routes[f"/news/articleView.html?idxno={STALE}"] = news_article(STALE, kst(date(2026, 6, 20)))
    return site


def active_source(store, base: str, *, signature: str = NEWS_SIG, hubs: list[str] | None = None, method: str = "STATIC",
                  scope: str = "TITLE_LINK", site_type: str = "NEWS") -> int:
    """워커가 등록을 끝낸 모습: ACTIVE 사이트 소스, 활성 설정 v1, 다음 주기 수집은 한 시간 뒤."""
    c = store.conn
    domain = base.split("://", 1)[1].split(":")[0]
    site_id = c.execute("INSERT INTO site (domain, base_url, site_type) VALUES (%s, %s, %s) RETURNING id",
                        (domain, base, site_type)).fetchone()["id"]
    source_id = c.execute("INSERT INTO source (site_id, name, status, method, next_run_at) "
                          "VALUES (%s, %s, 'ACTIVE', %s, now() + interval '1 hour') RETURNING id",
                          (site_id, domain, method)).fetchone()["id"]
    config = {"method": method, "hubs": hubs or [base + "/news/articleList.html"],
              "article_pattern": pattern_from_signature(signature), "site_domain": domain, "store_scope": scope,
              "collect_attachments": False}
    config_id = c.execute("INSERT INTO source_config_version (source_id, version, config, status) "
                          "VALUES (%s, 1, %s::jsonb, 'ACTIVE') RETURNING id",
                          (source_id, json.dumps(config, ensure_ascii=False))).fetchone()["id"]
    c.execute("UPDATE source SET active_config_version_id = %s WHERE id = %s", (config_id, source_id))
    return source_id


def request_backfill(store, source_id: int, period: tuple[date, date] = PERIOD) -> int:
    """백엔드 BackfillService.request와 같은 행."""
    return store.conn.execute("INSERT INTO backfill (source_id, date_from, date_to, requested_by) VALUES (%s, %s, %s, 1) "
                              "RETURNING id", (source_id, *period)).fetchone()["id"]


def run_until_idle(store, runner, settings, limit: int = 60) -> list[str]:
    """큐가 빈 워커처럼: 묶음을 넣고(main.backfill_idle) 집어 실행하기를 넣을 것이 없을 때까지."""
    statuses = []
    for _ in range(limit):
        if store.enqueue_backfill_chunk() is None:
            break
        statuses.append(process_one(store, runner, settings))
    return statuses


def backfill_row(store, backfill_id: int) -> dict:
    return q(store, "SELECT * FROM backfill WHERE id = %s", backfill_id)


def saved_ids(store) -> list[int]:
    rows = store.conn.execute("SELECT url FROM document ORDER BY url").fetchall()
    return sorted(int(r["url"].rsplit("=", 1)[-1]) for r in rows)


def article_hits(site: Site) -> list[str]:
    return [h for h in site.hits if "articleView" in h]


# ---------------------------------------------------------------------- 보관 사이트맵

def test_archive_sitemaps_overlapping_the_period_are_read_and_old_articles_saved(env):
    store, runner, settings, sites = env
    site = archive_site(sites)
    source_id = active_source(store, site.base)
    # 이미 수집한 기사 하나: 다시 열지 않고 '이미 있음'으로 센다
    store.upsert_document(source_id=source_id, job_id=0, url=f"{site.base}/news/articleView.html?idxno=300",
                          title="과거 기사 300번", author=None, section=None, published_at=kst(ARCHIVE[300]), body=None)
    backfill_id = request_backfill(store, source_id)

    statuses = run_until_idle(store, runner, settings)

    assert statuses == ["SUCCESS"]                       # 요청 100건 안에 끝난다 (한 묶음)
    b = backfill_row(store, backfill_id)
    assert b["status"] == "DONE" and b["method"] == "SITEMAP" and b["finished_at"] is not None
    assert saved_ids(store) == IN_PERIOD                 # 6월·9월 글과 발행일이 6월인 399는 저장하지 않는다
    assert b["found_count"] == len(IN_PERIOD) + 1        # 기간 안 주소 14건 + 399
    assert b["saved_count"] == len(IN_PERIOD) - 1 and b["skipped_count"] == 1 and b["out_of_range_count"] == 1
    assert b["failed_count"] == 0 and b["request_count"] == len(site.hits)
    assert "완료" in b["message"]
    # 기간에 걸치지 않는 보관 파일은 열지 않는다. 이미 있던 300번과 기간 밖 기사는 열지 않는다
    assert "/sitemap/archive-202605-202606.xml.gz" not in site.hits
    assert "/sitemap/archive-202609-202610.xml.gz" not in site.hits
    assert "/news/articleView.html?idxno=300" not in site.hits
    assert not any(f"idxno={i}" in h for h in article_hits(site) for i in (201, 202, 203, 501, 502, 503))
    assert len(article_hits(site)) == len(IN_PERIOD)     # 13건 + 399 (각각 한 번)
    # 언론사는 제목·링크·발행일만. 수집 시각은 발행 시각 (다이제스트·"오늘" 필터에 옛 기사가 새 기사처럼 끼지 않게)
    docs = store.conn.execute("SELECT title, body_text, published_at, collected_at FROM document "
                              "WHERE url LIKE %s", ("%idxno=4%",)).fetchall()
    assert docs and all(d["body_text"] is None and d["collected_at"] == d["published_at"] for d in docs)
    job = q(store, "SELECT status, found_count, new_count, note, backfill_id FROM crawl_job WHERE job_type = 'BACKFILL'")
    assert job["backfill_id"] == backfill_id and job["new_count"] == len(IN_PERIOD) - 1
    assert job["note"].startswith(f"과거 기사 채우기 #{backfill_id}")
    assert store.enqueue_backfill_chunk() is None        # 끝난 채우기는 묶음을 더 넣지 않는다


def test_small_chunks_save_progress_and_resume_without_refetching(env):
    store, runner, settings, sites = env
    site = archive_site(sites)
    source_id = active_source(store, site.base)
    backfill_id = request_backfill(store, source_id)
    small = JobRunner(store, runner.f, replace(settings, backfill_chunk=4), evidence=runner.evidence)

    assert process_one(store, small, settings) is None   # 큐에 넣기 전에는 집을 작업이 없다
    store.enqueue_backfill_chunk()
    assert process_one(store, small, settings) == "SUCCESS"
    b = backfill_row(store, backfill_id)
    assert b["status"] == "RUNNING" and b["chunk_count"] == 1 and b["request_count"] == 4
    assert b["cursor"]["phase"] == "SITEMAP"             # 진행 위치가 남는다

    statuses = run_until_idle(store, small, settings)
    b = backfill_row(store, backfill_id)
    assert b["status"] == "DONE" and len(statuses) >= 4
    assert b["chunk_count"] == len(statuses) + 1
    assert saved_ids(store) == IN_PERIOD
    hits = article_hits(site)
    assert len(hits) == len(set(hits)) == len(IN_PERIOD) + 1   # 끊었다 이어도 같은 기사를 두 번 열지 않는다
    assert site.hits.count("/sitemap/archive-202607-202608.xml.gz") == 1
    assert q(store, "SELECT count(*) AS c FROM crawl_job WHERE job_type = 'BACKFILL' AND status = 'SUCCESS'")["c"] == \
        len(statuses) + 1


# ---------------------------------------------------------------------- 양보·취소

def test_backfill_yields_to_queued_jobs_and_to_the_sources_own_crawl(env):
    store, runner, settings, sites = env
    site = archive_site(sites)
    source_id = active_source(store, site.base)
    other = active_source(store, "http://other.example")
    backfill_id = request_backfill(store, source_id)

    # 다른 작업이 대기 중이면 묶음을 넣지 않는다
    crawl = q(store, "INSERT INTO crawl_job (source_id, job_type, trigger) VALUES (%s, 'CRAWL', 'SCHEDULE') RETURNING id",
              other)["id"]
    assert store.enqueue_backfill_chunk() is None
    # 묶음이 먼저 들어와 있어도 평소 작업을 먼저 집는다
    store.conn.execute("INSERT INTO crawl_job (source_id, job_type, trigger, backfill_id, created_at) "
                       "VALUES (%s, 'BACKFILL', 'MANUAL', %s, now() - interval '1 hour')", (source_id, backfill_id))
    assert store.claim_job("w", 5).job_type == "CRAWL"
    store.conn.execute("UPDATE crawl_job SET status = 'SUCCESS', finished_at = now() WHERE id = %s", (crawl,))

    # 묶음 도중 평소 작업이 들어오면 다음 주소 전에 멈춘다
    get, opened = runner.f.get, []

    def get_and_queue(url):
        page = get(url)
        if "articleView" in url:
            opened.append(url)
            if len(opened) == 2:
                store.conn.execute("INSERT INTO crawl_job (source_id, job_type, trigger) VALUES (%s, 'CRAWL', 'SCHEDULE')",
                                   (other,))
        return page

    runner.f.get = get_and_queue
    try:
        assert process_one(store, runner, settings) == "SUCCESS"
    finally:
        runner.f.get = get
    job = q(store, "SELECT note FROM crawl_job WHERE job_type = 'BACKFILL' ORDER BY id DESC LIMIT 1")
    assert "양보" in job["note"]
    b = backfill_row(store, backfill_id)
    assert b["status"] == "RUNNING" and b["saved_count"] == 2 and len(opened) == 2
    assert "잠시 멈춤" in b["message"]
    store.conn.execute("UPDATE crawl_job SET status = 'SUCCESS', finished_at = now() WHERE job_type = 'CRAWL'")

    # 이 소스의 주기 수집 차례가 오면: 새 묶음을 넣지 않고(스케줄러가 CRAWL을 넣게 비워 둔다), 도는 묶음은 바로 멈춘다
    store.conn.execute("UPDATE source SET next_run_at = now() - interval '1 minute' WHERE id = %s", (source_id,))
    assert store.enqueue_backfill_chunk() is None
    store.conn.execute("INSERT INTO crawl_job (source_id, job_type, trigger, backfill_id) VALUES (%s, 'BACKFILL', 'MANUAL', %s)",
                       (source_id, backfill_id))
    before = len(article_hits(site))
    assert process_one(store, runner, settings) == "SUCCESS"
    assert len(article_hits(site)) == before
    assert "주기 수집 차례" in q(store, "SELECT note FROM crawl_job ORDER BY id DESC LIMIT 1")["note"]

    # 주기 수집이 끝나면 이어서 끝까지 한다
    store.conn.execute("UPDATE source SET next_run_at = now() + interval '1 hour' WHERE id = %s", (source_id,))
    run_until_idle(store, runner, settings)
    assert backfill_row(store, backfill_id)["status"] == "DONE"
    assert saved_ids(store) == IN_PERIOD


def test_cancel_stops_the_running_chunk_and_no_more_chunks_are_queued(env):
    store, runner, settings, sites = env
    site = archive_site(sites)
    source_id = active_source(store, site.base)
    backfill_id = request_backfill(store, source_id)

    get = runner.f.get

    def get_and_cancel(url):
        page = get(url)
        if "articleView" in url:   # 백엔드 BackfillService.cancel과 같은 갱신
            store.conn.execute("UPDATE backfill SET status = 'CANCELLED', finished_at = now() WHERE id = %s", (backfill_id,))
        return page

    runner.f.get = get_and_cancel
    try:
        store.enqueue_backfill_chunk()
        assert process_one(store, runner, settings) == "SUCCESS"
    finally:
        runner.f.get = get
    b = backfill_row(store, backfill_id)
    assert b["status"] == "CANCELLED" and b["saved_count"] == 1 and len(article_hits(site)) == 1
    assert "취소" in q(store, "SELECT note FROM crawl_job ORDER BY id DESC LIMIT 1")["note"]
    assert store.enqueue_backfill_chunk() is None
    assert len(saved_ids(store)) == 1                     # 이미 저장한 문서는 그대로

    # 대기 중이던 묶음이 취소 뒤에 집혀도 아무것도 하지 않는다
    store.conn.execute("INSERT INTO crawl_job (source_id, job_type, trigger, backfill_id) VALUES (%s, 'BACKFILL', 'MANUAL', %s)",
                       (source_id, backfill_id))
    assert process_one(store, runner, settings) == "SUCCESS"
    assert len(article_hits(site)) == 1


def test_rate_limit_stops_backfill_and_blocks_source_without_retrying(env):
    store, runner, settings, sites = env
    site = archive_site(sites)
    site.routes["/news/articleView.html?idxno=301"] = (429, "text/html", "too many requests")
    source_id = active_source(store, site.base)
    backfill_id = request_backfill(store, source_id)

    statuses = run_until_idle(store, runner, settings)

    assert statuses == ["FAILED"]
    b = backfill_row(store, backfill_id)
    assert b["status"] == "FAILED" and "RATE_LIMITED" in b["message"]
    src = q(store, "SELECT status, status_reason FROM source WHERE id = %s", source_id)
    assert src["status"] == "BLOCKED" and src["status_reason"] == "RATE_LIMITED"
    assert q(store, "SELECT code FROM crawl_error")["code"] == "RATE_LIMITED"
    assert site.hits.count("/news/articleView.html?idxno=301") == 1
    assert store.enqueue_backfill_chunk() is None          # BLOCKED 소스는 묶음을 넣지 않는다


# ---------------------------------------------------------------------- 목록 페이지 역순

def board_site(sites: list[Site], *, dated: bool, total: int, step_days: int, last: date = date(2026, 10, 18),
               pinned: bool = False) -> Site:
    """공공기관 게시판: /board/list.do?bbsId=B1(&page=N), 쪽마다 5건, 글 n(1이 최신)의 등록일 = last - step_days*n.
    dated면 목록 행에 날짜가 보이고 아니면 상세에만 있다. pinned면 모든 쪽 맨 위에 고정 공지 두 건(최근·오래된)."""
    site = Site({}); sites.append(site)
    pages = (total + 4) // 5
    posted = {n: last - timedelta(days=step_days * n) for n in range(1, total + 1)}
    pins = {9000: date(2026, 10, 1), 9001: date(2025, 1, 1)} if pinned else {}

    def row(nid: int, d: date) -> str:
        return (f'<li><a href="/board/view.do?bbsId=B1&amp;nttId={nid}">게시글 {nid}번 안내</a>'
                + (f"<span>{d.strftime('%Y.%m.%d')}</span>" if dated else "") + "</li>")

    for p in range(1, pages + 1):
        rows = "".join(row(n, d) for n, d in pins.items())
        rows += "".join(row(n, posted[n]) for n in range((p - 1) * 5 + 1, min(p * 5, total) + 1))
        paging = "".join(f'<a href="/board/list.do?bbsId=B1&amp;page={k}">{k}</a>' for k in range(1, pages + 1))
        html = f'<html><head><title>보도자료</title></head><body><ul class="board">{rows}</ul><div class="paging">{paging}</div></body></html>'
        key = "/board/list.do?bbsId=B1" + ("" if p == 1 else f"&page={p}")
        site.routes[key] = (200, "text/html; charset=utf-8", html)
    for nid, d in {**posted, **pins}.items():
        site.routes[f"/board/view.do?bbsId=B1&nttId={nid}"] = (200, "text/html; charset=utf-8", f"""<html>
            <head><title>게시글 {nid}번 안내</title></head><body><div class="board_view"><h3>게시글 {nid}번 안내</h3>
            <span>등록일 {d.strftime('%Y.%m.%d')} 10:00</span><p>{BODY}</p></div></body></html>""")
    site.routes["/robots.txt"] = (404, "text/plain", "")
    site.posted = posted
    return site


def board_source(store, site: Site) -> int:
    return active_source(store, site.base, signature="/board/view.do?bbsId=*&nttId={n}",
                         hubs=[site.base + "/board/list.do?bbsId=B1"], scope="FULL", site_type="PUBLIC")


def board_ids(store) -> list[int]:
    return sorted(int(r["url"].rsplit("=", 1)[-1]) for r in store.conn.execute("SELECT url FROM document").fetchall())


def list_pages_requested(site: Site) -> list[int]:
    out = []
    for h in site.hits:
        if h.startswith("/board/list.do"):
            out.append(int(h.rsplit("page=", 1)[-1]) if "page=" in h else 1)
    return out


def test_dated_list_pages_are_walked_backwards_until_older_than_the_period(env):
    store, runner, settings, sites = env
    # 글 45건, 4일 간격: 1번 10-14 … 12번 08-31(기간 끝) … 27번 07-02 … 31번 06-16. 쪽마다 5건 → 7쪽에서 멈춰야 한다
    site = board_site(sites, dated=True, total=45, step_days=4, pinned=True)
    source_id = board_source(store, site)
    backfill_id = request_backfill(store, source_id)

    run_until_idle(store, runner, settings)

    b = backfill_row(store, backfill_id)
    assert b["status"] == "DONE" and b["method"] == "LIST"
    want = sorted(n for n, d in site.posted.items() if PERIOD[0] <= d <= PERIOD[1])
    assert want == list(range(12, 28))
    assert board_ids(store) == want
    # 1~2쪽(기간보다 새 글)은 보기만 하고 기사는 열지 않는다. 7쪽 새 글이 모두 기간 전이라 멈추고 8·9쪽은 열지 않는다.
    # 모든 쪽에 고정된 공지(최근·오래된)는 멈춤 판단에 끼지 않는다
    assert list_pages_requested(site) == [1, 2, 3, 4, 5, 6, 7]
    opened = {int(h.rsplit("=", 1)[-1]) for h in site.hits if "view.do" in h}
    assert opened == set(want)
    assert "/board/list.do?bbsId=B1&page=2" in site.hits     # 쪽 링크에서 찾은 page 매개변수로 넘긴다
    assert "7쪽 글이 모두 2026-07-01보다 일러 멈춤" in " ".join(b["cursor"]["notes"])


def test_undated_list_stops_after_a_page_whose_articles_are_all_older(env):
    store, runner, settings, sites = env
    # 글 20건, 10일 간격, 목록에 날짜 없음: 1~5번 10-08~09-18, 6~10번 09-08~08-19, 11~15번 08-09~07-20, 16~20번 …
    site = board_site(sites, dated=False, total=20, step_days=10)
    source_id = board_source(store, site)
    backfill_id = request_backfill(store, source_id, (date(2026, 8, 1), date(2026, 9, 10)))

    run_until_idle(store, runner, settings)

    b = backfill_row(store, backfill_id)
    assert b["status"] == "DONE"
    assert board_ids(store) == [4, 5, 6, 7]
    # 3쪽 글을 열어 보니 모두 08-01보다 일러 4쪽은 열지 않는다. 기간 밖 글은 열었지만 저장하지 않는다
    assert list_pages_requested(site) == [1, 2, 3]
    assert b["out_of_range_count"] == 11 and b["saved_count"] == 4


def test_feed_only_source_without_archive_falls_back_to_sections_or_finishes(env):
    store, runner, settings, sites = env
    site = Site({"/robots.txt": (404, "text/plain", ""),
                 "/rss.xml": (200, "application/rss+xml", "<?xml version='1.0'?><rss><channel></channel></rss>")})
    sites.append(site)
    source_id = active_source(store, site.base, hubs=[site.base + "/rss.xml"], method="RSS")
    backfill_id = request_backfill(store, source_id)

    assert run_until_idle(store, runner, settings) == ["SUCCESS"]
    b = backfill_row(store, backfill_id)
    assert b["status"] == "DONE" and b["found_count"] == 0
    assert "사이트맵이 없습니다" in " ".join(b["cursor"]["notes"])


# ---------------------------------------------------------------------- 이슈 묶기: 옛 기사는 지금 이슈에 붙지 않는다

def test_backfilled_article_does_not_join_a_current_issue(env):
    store, runner, settings, sites = env
    source_id = active_source(store, "http://news.example")
    other = active_source(store, "http://wire.example")
    title = "반도체 공장 화재로 생산 라인 가동 중단"
    now = datetime.now(timezone.utc)
    for sid, n in ((source_id, 1), (other, 2)):   # 지금 이슈: 두 언론사의 같은 보도
        store.upsert_document(source_id=sid, job_id=0, url=f"http://news.example/news/articleView.html?idxno={n}",
                              title=title, author=None, section=None, published_at=now - timedelta(hours=n), body=None)
    old = kst(date(2026, 7, 15))   # 석 달 전 같은 제목의 다른 사건 (채우기로 넣은 문서: 수집 시각 = 발행 시각)
    store.upsert_document(source_id=source_id, job_id=0, url="http://news.example/news/articleView.html?idxno=3",
                          title=title, author=None, section=None, published_at=old, body=None, collected_at=old)

    indexer.index_pending(store, settings)

    rows = store.conn.execute("SELECT d.url, i.issue_id FROM issue_document i JOIN document d ON d.id = i.document_id "
                              "ORDER BY d.url").fetchall()
    issue_of = {r["url"].rsplit("=", 1)[-1]: r["issue_id"] for r in rows}
    assert issue_of["1"] == issue_of["2"] != issue_of["3"]
    old_issue = q(store, "SELECT first_seen_at, last_seen_at, doc_count FROM issue WHERE id = %s", issue_of["3"])
    assert old_issue["doc_count"] == 1 and old_issue["first_seen_at"] == old
