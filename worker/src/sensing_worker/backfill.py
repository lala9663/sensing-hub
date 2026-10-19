"""과거 기사 채우기(BACKFILL): 등록 전에 나온 기사를 기간(date_from~date_to)을 정해 천천히 가져온다.

평소 수집은 허브에 지금 보이는 최신 기사만 본다 (RSS 50건 안팎, 뉴스 사이트맵 이틀 안팎). 채우기는 그 이전 기사를 찾는다.

찾는 방법 (앞에서 기간을 덮으면 뒤로 가지 않는다)
1. SITEMAP: robots.txt의 Sitemap 줄(과 설정의 사이트맵 허브)에서 출발해 색인을 따라가며 기간에 걸치는 보관 사이트맵(.xml·.xml.gz)만
   연다. 하위 사이트맵은 파일 이름의 날짜(donga-sitemap-202609-202610.xml.gz → 2026-09-01~10-31)로, 이름에 날짜가 없으면 색인의
   lastmod로 고른다 (lastmod가 기간 시작보다 이르면 그 안의 글도 모두 이르다). 항목은 news:publication_date → lastmod로 기간을 본다.
2. LIST: 사이트맵에 기간의 글이 없거나 사이트맵이 기간 시작까지 닿지 않으면 소스의 목록 페이지(HTML 허브)를 쪽 번호로 거꾸로
   넘긴다. 쪽 번호 매개변수는 목록의 쪽 링크(?page=2, /page/2, fn_egov_link_page(2) + hidden pageIndex)에서 찾는다. 앞 쪽에 없던
   글(고정 공지 제외)의 날짜가 모두 기간 시작보다 이르면 멈춘다. 목록에 날짜가 없으면 그 쪽 글을 열어 본 발행일로 판단한다.

기사 판정·추출·저장·키워드 대조는 평소 수집(jobs.JobRunner._crawl)과 같은 함수를 쓴다 — 기사 패턴(article_pattern 또는 기사 종류
article_patterns)·matches·canonical_article_url, extract_article(설정 selectors·date_format), upsert_document(언론사는 제목·링크·
발행일만, 본문 미저장), _match_watches, 첨부. 열어 본 발행일이 기간 밖이거나 없으면 저장하지 않는다(OUT_OF_RANGE).

저속·저우선
- 한 묶음(crawl_job BACKFILL 하나)에 요청 settings.backfill_chunk(기본 100)건까지 하고 진행 위치를 남긴다: cursor(사이트맵 대기열·목록
  쪽 번호)와 backfill_url(찾은 주소별 PENDING·SAVED·SKIPPED·OUT_OF_RANGE·FAILED). 다음 묶음은 큐가 빈 워커가 넣는다(main.backfill_idle).
- 묶음 도중에도 주소마다 확인해 평소 작업(CRAWL·ONBOARD)이 대기하거나 이 소스의 주기 수집 차례가 오면 멈추고 양보한다. 취소도 같다.
- 도메인 간격·robots는 fetcher가 평소와 같이 지킨다. 차단 신호(봇 확인·429·로그인)는 우회하지 않고 즉시 멈추고 소스를 BLOCKED로 둔다.
- 이미 있는 문서(url_hash)는 열지 않는다. 새 문서의 collected_at은 발행 시각으로 둔다 — 다이제스트·문서 화면의 "오늘·3일·7일"·
  색인·요약 순서처럼 수집 시각을 "새로 들어온 것"으로 쓰는 곳에 옛 기사가 새 기사처럼 끼지 않게. 색인·이슈 묶기·요약은 평소 유휴
  처리가 하고, 이슈는 발행 시각 기준 창(issues.doc_time)으로 묶여 옛 기사가 지금 이슈에 붙지 않는다.
"""

from __future__ import annotations

import calendar
import html as htmllib
import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .codes import CrawlFailure
from .db import Job, sha256
from .discovery import canonical_article_url, looks_like_feed, looks_like_sitemap, matches, same_site
from .extract import Article, _parse_date, extract_article, links_with_dates

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
# lastmod는 글을 고친 시각일 수 있다: 기간 끝보다 이만큼 늦은 것까지는 넣고 상세에서 발행일로 확인한다
LASTMOD_SLACK_DAYS = 3
# 사이트맵의 가장 이른 글이 기간 시작보다 이만큼 넘게 늦으면 사이트맵이 기간을 다 덮지 못한 것으로 보고 목록 페이지로 이어 찾는다
COVERAGE_SLACK_DAYS = 3
MAX_SITEMAP_DEPTH = 3      # 색인 → 색인 → 사이트맵
ARTICLE_BATCH = 10         # 한 번에 꺼내 여는 주소 수 (주소마다 양보·취소를 확인한다)
RECORDED_ERRORS = 3        # 묶음 하나에서 crawl_error로 남기는 기사 실패 수 (나머지는 수만 센다)
MAX_NOTES = 8
CANCELLED = "취소됨"        # 묶음을 멈춘 이유: 사용자가 취소 (나머지 이유는 양보·상한)


# ---------------------------------------------------------------------- 기사 판정 (평소 수집과 같은 규칙)

def article_kinds(config: dict) -> list[dict]:
    """켜진 기사 판정 패턴 목록. 설정의 article_patterns(사이트 하나에 기사 종류 여러 개)가 있으면 켜진 것, 없으면 article_pattern
    하나짜리 이름 없는 종류. 기사 종류 브랜치의 discovery.article_kinds와 같은 규칙이다 (합친 뒤에는 그것 하나를 쓰면 된다)."""
    kinds = config.get("article_patterns")
    if not kinds:
        pattern = config.get("article_pattern")
        return [{**pattern, "name": None, "hubs": list(config.get("hubs") or [])}] if pattern else []
    return [{**k, "hubs": list(k.get("hubs") or config.get("hubs") or [])} for k in kinds if k.get("enabled", True)]


def match_article(url: str, kinds: list[dict], domain: str) -> tuple[str, str | None] | None:
    """기사 주소면 (정규 주소, 종류 이름). 같은 사이트이고 켜진 종류의 패턴에 맞아야 한다 (앞 종류가 먼저)."""
    if not same_site(url, domain):
        return None
    for kind in kinds:
        if matches(url, kind):
            return canonical_article_url(url, kind), kind.get("name")
    return None


# ---------------------------------------------------------------------- 날짜

def kst_day(raw: str | None) -> date | None:
    """사이트맵 lastmod·목록 행 날짜 → 한국 날짜. ISO 형식은 빠른 길로 읽는다 (보관 사이트맵은 한 파일에 수만 건이다)."""
    if not raw:
        return None
    text = raw.strip()
    dt: datetime | None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        dt = _parse_date(text)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt.astimezone(KST).date()


def published_day(art: Article) -> date | None:
    return art.published_at.astimezone(KST).date() if art.published_at else None


# 파일 이름의 날짜 조각: 2026 | 202609 | 2026-09 | 20260915 | 2026/09/15
_SPAN_TOKEN_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?:[-_./]?(0[1-9]|1[0-2])(?:[-_./]?(0[1-9]|[12]\d|3[01]))?)?(?!\d)")


def sitemap_span(url: str, today: date | None = None) -> tuple[date, date] | None:
    """사이트맵 주소(경로·쿼리)의 날짜로 본 기간. donga-sitemap-202609-202610.xml.gz → (2026-09-01, 2026-10-31).
    날짜 조각이 둘 이상이면 처음 것의 시작부터 마지막 것의 끝까지. 연도만 있는 조각은 올해 이하일 때만 연도로 본다
    (sitemap-2048.xml 같은 쪽 번호를 연도로 읽지 않게). 날짜가 없으면 None."""
    parts = urlsplit(url)
    text = parts.path + ("?" + parts.query if parts.query else "")
    this_year = (today or datetime.now(KST).date()).year
    spans: list[tuple[date, date]] = []
    for m in _SPAN_TOKEN_RE.finditer(text):
        y, mo, d = int(m.group(1)), m.group(2), m.group(3)
        if mo is None:
            if not 1990 <= y <= this_year:
                continue
            spans.append((date(y, 1, 1), date(y, 12, 31)))
        elif d is None:
            last = calendar.monthrange(y, int(mo))[1]
            spans.append((date(y, int(mo), 1), date(y, int(mo), last)))
        else:
            try:
                day = date(y, int(mo), int(d))
            except ValueError:
                continue
            spans.append((day, day))
    if not spans:
        return None
    return min(s for s, _ in spans), max(e for _, e in spans)


# ---------------------------------------------------------------------- 사이트맵

@dataclass
class SitemapEntry:
    loc: str
    lastmod: str | None = None
    published: str | None = None     # news:publication_date


_BLOCK_RE = re.compile(r"<(url|sitemap)\b[^>]*>(.*?)</\1\s*>", re.S | re.I)
_LOC_RE = re.compile(r"<loc\b[^>]*>(.*?)</loc\s*>", re.S | re.I)
_LASTMOD_RE = re.compile(r"<lastmod\b[^>]*>(.*?)</lastmod\s*>", re.S | re.I)
_PUBDATE_RE = re.compile(r"<(?:[\w-]+:)?publication_date\b[^>]*>(.*?)</(?:[\w-]+:)?publication_date\s*>", re.S | re.I)


def _xml_text(m: re.Match | None) -> str | None:
    if m is None:
        return None
    v = m.group(1).strip()
    if v.startswith("<![CDATA["):
        v = v[9:]
        if v.endswith("]]>"):
            v = v[:-3]
    return htmllib.unescape(v.strip()) or None


def parse_sitemap(text: str) -> tuple[bool, list[SitemapEntry]]:
    """(색인인지, 항목 목록). 정규식으로 읽는다: 수만 건짜리 보관 사이트맵도 빠르고 메모리를 적게 쓴다."""
    is_index = "<sitemapindex" in text[:5000].lower()
    out: list[SitemapEntry] = []
    for m in _BLOCK_RE.finditer(text):
        body = m.group(2)
        loc = _xml_text(_LOC_RE.search(body))
        if loc:
            out.append(SitemapEntry(loc, _xml_text(_LASTMOD_RE.search(body)), _xml_text(_PUBDATE_RE.search(body))))
    return is_index, out


def child_wanted(entry: SitemapEntry, date_from: date, date_to: date, today: date | None = None) -> bool:
    """색인의 하위 사이트맵을 열지. 이름에 날짜가 있으면 그 기간이 겹칠 때, 없으면 lastmod가 기간 시작 이후일 때(또는 lastmod가 없을 때)."""
    span = sitemap_span(entry.loc, today)
    if span:
        return span[0] <= date_to and span[1] >= date_from
    day = kst_day(entry.lastmod)
    return day is None or day >= date_from


def child_order(entry: SitemapEntry, today: date | None = None) -> date:
    """하위 사이트맵을 여는 순서의 키 (최근 것부터): 이름의 기간 끝 → lastmod."""
    span = sitemap_span(entry.loc, today)
    return (span[1] if span else None) or kst_day(entry.lastmod) or date.min


def entry_day(entry: SitemapEntry) -> date | None:
    return kst_day(entry.published) or kst_day(entry.lastmod)


def entry_in_period(entry: SitemapEntry, date_from: date, date_to: date) -> bool:
    """사이트맵 항목을 후보로 넣을지. 발행일이 있으면 그것으로, lastmod뿐이면 끝에 여유를 두고, 날짜가 없으면 넣는다(상세에서 확인)."""
    pub = kst_day(entry.published)
    if pub:
        return date_from <= pub <= date_to
    mod = kst_day(entry.lastmod)
    if mod:
        return date_from <= mod <= date_to + timedelta(days=LASTMOD_SLACK_DAYS)
    return True


# ---------------------------------------------------------------------- 목록 페이지 쪽 번호

# 쪽 번호로 흔한 매개변수 이름 (소문자로 비교)
PAGE_KEYS = ("page", "pageindex", "pageno", "pagenum", "page_no", "pagenumber", "cpage", "curpage", "currentpage",
             "currpage", "pg", "p", "npage", "pageidx", "gotopage", "page_index")
# 스크립트 쪽 이동: fn_egov_link_page(2), goPage('3'), linkPage(4), fnPaging(5)
_JS_PAGE_RE = re.compile(r"(\w*(?:page|paging|link|go|move)\w*)\s*\(\s*['\"]?(\d{1,4})['\"]?\s*\)", re.I)


def find_page_param(html: str, url: str, id_keys: set[str] = frozenset()) -> dict | None:
    """목록 페이지의 쪽 번호를 넘기는 방법. {"kind": "query", "name": "page"} | {"kind": "path", "template": "/news/page/{n}"} | None.

    1) 같은 경로로 가는 링크 중 숫자 값(2~50)만 다른 매개변수 (기사 식별 매개변수 id_keys는 뺀다, 흔한 이름을 먼저)
    2) 워드프레스형 /page/2
    3) 스크립트 쪽 이동(fn_egov_link_page(2) 등)이 둘 이상이면 폼의 쪽 번호 입력(hidden pageIndex 등) 이름, 없으면 pageIndex·page
    """
    soup = BeautifulSoup(html, "lxml")
    hub = urlsplit(url)
    hub_q = dict(parse_qsl(hub.query, keep_blank_values=True))
    lowered_ids = {k.lower() for k in id_keys}
    votes: Counter[str] = Counter()
    path_hits = js_hits = 0
    base_path = hub.path.rstrip("/")
    path_re = re.compile(re.escape(base_path) + r"/page/(\d+)/?$")
    for a in soup.find_all("a"):
        href = (a.get("href") or "").strip()
        script = href if href.lower().startswith("javascript:") else (a.get("onclick") or "")
        if script and _JS_PAGE_RE.search(script):
            js_hits += 1
        if not href or href.lower().startswith(("javascript:", "#", "mailto:", "tel:")):
            continue
        target = urlsplit(urljoin(url, href))
        if target.netloc != hub.netloc:
            continue
        if target.path == hub.path:
            q = dict(parse_qsl(target.query, keep_blank_values=True))
            for key, value in q.items():
                if (not value.isdigit() or not 2 <= int(value) <= 50 or hub_q.get(key) == value
                        or key.lower() in lowered_ids):
                    continue
                # 다른 매개변수(쪽 번호류 말고)는 목록 주소와 같아야 한다 (다른 게시판으로 가는 링크가 아니다)
                if all(q.get(k) == v for k, v in hub_q.items() if k != key and k.lower() not in PAGE_KEYS):
                    votes[key] += 1
        elif path_re.match(target.path):
            path_hits += 1
    if votes:
        best = max(votes, key=lambda k: (k.lower() in PAGE_KEYS, votes[k]))
        return {"kind": "query", "name": best}
    if path_hits:
        return {"kind": "path", "template": base_path + "/page/{n}"}
    if js_hits >= 2:
        for inp in soup.find_all("input"):
            name = inp.get("name") or ""
            if name.lower() in PAGE_KEYS:
                return {"kind": "query", "name": name}
        return {"kind": "query", "name": "pageIndex" if "egov" in html.lower() or "pageindex" in html.lower() else "page"}
    return None


def page_url(url: str, param: dict, n: int) -> str:
    """목록 주소의 n쪽."""
    parts = urlsplit(url)
    if param["kind"] == "path":
        return urlunsplit((parts.scheme, parts.netloc, param["template"].format(n=n), parts.query, ""))
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != param["name"]]
    q.append((param["name"], str(n)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), ""))


# ---------------------------------------------------------------------- 저장 (평소 수집과 같은 저장)

def store_article(runner, job: Job, config: dict, url: str, art: Article, *, section: str | None = None,
                  collected_at: datetime | None = None) -> tuple[int, str]:
    """JobRunner._crawl과 같은 저장: 저장 범위(언론사 TITLE_LINK는 본문 미저장), 키워드 감시 대조(본문은 대조에만), 첨부."""
    scope = config.get("store_scope", "FULL")
    doc_id, result = runner.db.upsert_document(
        source_id=job.source_id, job_id=job.id, url=url, title=art.title, author=art.author,
        section=section or art.section, published_at=art.published_at,
        body=None if scope == "TITLE_LINK" else art.body, collected_at=collected_at)
    if result in ("NEW", "UPDATED"):
        runner._match_watches(doc_id, art.title, art.body)
    if config.get("collect_attachments"):
        for att_url, name in art.attachments:
            runner.db.add_attachment(doc_id, att_url, name)
    return doc_id, result


# ---------------------------------------------------------------------- 묶음 실행

def run_backfill(runner, job: Job, source: dict) -> str:
    """BACKFILL 작업 하나(한 묶음)를 실행한다. 작업 상태를 돌려준다."""
    db = runner.db
    bf = db.load_backfill(job.id, job.source_id)
    if bf is None or bf["status"] not in ("QUEUED", "RUNNING"):
        db.finish_job(job.id, "SUCCESS", note="과거 기사 채우기가 이미 끝났거나 취소됐습니다.")
        return "SUCCESS"
    config = source.get("config") or {}
    if source.get("kind", "SITE") != "SITE" or config.get("method") == "API" or not article_kinds(config):
        message = "사이트 수집 설정(기사 패턴)이 없어 과거 기사를 채울 수 없습니다."
        db.finish_backfill(bf["id"], "FAILED", message)
        db.finish_job(job.id, "FAILED", errors=1, note=message)
        return "FAILED"
    db.start_backfill(bf["id"])
    return BackfillChunk(runner, job, source, config, bf).run()


class BackfillChunk:
    def __init__(self, runner, job: Job, source: dict, config: dict, bf: dict):
        self.r = runner
        self.db = runner.db
        self.f = runner.f
        self.s = runner.s
        self.job = job
        self.source = source
        self.config = config
        self.bf_id = bf["id"]
        self.date_from: date = bf["date_from"]
        self.date_to: date = bf["date_to"]
        self.method: str | None = bf["method"]
        self.cursor: dict = dict(bf["cursor"] or {})
        self.kinds = article_kinds(config)
        self.named = len(config.get("article_patterns") or []) > 1
        self.domain = config.get("site_domain") or source["domain"]
        self.start_requests = self.f.request_count
        self.found = self.saved = self.skipped = self.out = self.failed = 0
        self.recorded = 0

    # -------------------------------------------------------------- 흐름
    def run(self) -> str:
        stop: str | None = None
        done = False
        try:
            while True:
                stop = self._stop_reason()
                if stop:
                    break
                rows = self.db.next_backfill_urls(self.bf_id, ARTICLE_BATCH)
                if rows:
                    for row in rows:
                        stop = self._stop_reason()
                        if stop:
                            break
                        self._article(row)
                    if stop:
                        break
                    continue
                if self.cursor.get("phase") == "DONE":
                    done = True
                    break
                self._discover_step()
        except CrawlFailure as e:
            return self._blocked(e)
        return self._finish(stop, done)

    def _used(self) -> int:
        return self.f.request_count - self.start_requests

    def _stop_reason(self) -> str | None:
        if self._used() >= self.s.backfill_chunk:
            return "BUDGET"
        if self.db.backfill_status(self.bf_id) != "RUNNING":
            return CANCELLED
        return self.db.backfill_yield_reason(self.job.source_id)

    def _finish(self, stop: str | None, done: bool) -> str:
        pending = self.db.pending_backfill_count(self.bf_id)
        # 취소됐으면 백엔드가 남긴 취소 메시지를 덮지 않는다
        message = None if done or stop == CANCELLED else self._progress(stop)
        self.db.save_backfill(self.bf_id, method=self.method, cursor=self.cursor, requests=self._used(), message=message)
        if done and pending == 0:
            row = self.db.load_backfill(self.job.id, self.job.source_id) or {}
            self.db.finish_backfill(self.bf_id, "DONE", done_message(row, self.cursor))
        status = "PARTIAL" if self.failed else "SUCCESS"
        note = f"과거 기사 채우기 #{self.bf_id}: 새 {self.saved} · 이미 {self.skipped} · 남은 {pending}"
        if stop and stop != "BUDGET":
            note += f" · {stop}"
        self.db.finish_job(self.job.id, status, self.found, self.saved, self.failed,
                           self.source.get("active_config_version_id"), note=note)
        log.info("backfill %s source=%s chunk: requests=%s found=%s saved=%s skipped=%s out=%s failed=%s pending=%s "
                 "phase=%s stop=%s", self.bf_id, self.job.source_id, self._used(), self.found, self.saved, self.skipped,
                 self.out, self.failed, pending, self.cursor.get("phase"), stop)
        return status

    def _blocked(self, e: CrawlFailure) -> str:
        """차단 신호: 평소 수집과 같이 우회하지 않고 멈추고 소스를 BLOCKED로 둔다 (관리자 재점검으로만 다시)."""
        self.r._record(self.job, e)
        if e.blocking:
            self.db.set_source_status(self.job.source_id, "BLOCKED", e.code.value)
            message = f"사이트가 자동 접근을 막아 멈췄습니다 ({e.code.value}: {e}). 소스는 차단(BLOCKED)으로 두었습니다."
        else:
            message = f"멈췄습니다 ({e.code.value}: {e})."
        self.db.save_backfill(self.bf_id, method=self.method, cursor=self.cursor, requests=self._used(), message=None)
        self.db.finish_backfill(self.bf_id, "FAILED", message)
        self.db.finish_job(self.job.id, "FAILED", self.found, self.saved, self.failed + 1,
                           self.source.get("active_config_version_id"), note=f"과거 기사 채우기 #{self.bf_id} 중단: {e.code.value}")
        log.warning("backfill %s source=%s stopped: %s %s", self.bf_id, self.job.source_id, e.code.value, e)
        return "FAILED"

    def _progress(self, stop: str | None) -> str:
        c = self.cursor
        if c.get("phase") == "SITEMAP":
            where = f"보관 사이트맵 {c.get('files', 0)}개 읽음, 열 사이트맵 {len(c.get('queue') or [])}개 남음"
        elif c.get("phase") == "LIST":
            hubs = c.get("hubs") or []
            where = f"목록 페이지 {min(c.get('hub', 0) + 1, len(hubs))}/{len(hubs)}번째 허브 {c.get('page', 0)}쪽까지 봄"
        else:
            where = "기사를 여는 중"
        if stop and stop != "BUDGET":
            where += f" · 잠시 멈춤: {stop}"
        return where

    def _note(self, text: str) -> None:
        notes = list(self.cursor.get("notes") or [])
        notes.append(text[:200])
        self.cursor["notes"] = notes[-MAX_NOTES:]

    # -------------------------------------------------------------- 기사 하나
    def _article(self, row: dict) -> None:
        url, url_hash = row["url"], row["url_hash"]
        if self.db.known_urls([url_hash]):        # 찾은 뒤 평소 수집이 먼저 저장했다
            self._mark(url_hash, "SKIPPED")
            return
        try:
            page = self.f.get(url)
        except CrawlFailure as e:
            if e.blocking:
                raise
            e.stage = "DETAIL"
            if self.recorded < RECORDED_ERRORS:
                self.r._record(self.job, e)
                self.recorded += 1
            self._mark(url_hash, "FAILED")
            return
        art = extract_article(page.text, page.url, self.config.get("selectors"), date_hint=row["date_hint"],
                              date_format=self.config.get("date_format"))
        day = published_day(art)
        self._seen_day(day)
        if day is None or not self.date_from <= day <= self.date_to:
            self._mark(url_hash, "OUT_OF_RANGE")
            return
        now = datetime.now(timezone.utc)
        _, result = store_article(self.r, self.job, self.config, url, art,
                                  section=row["kind"] if self.named else None,
                                  collected_at=min(art.published_at, now))
        self._mark(url_hash, "SAVED" if result == "NEW" else "SKIPPED")

    def _mark(self, url_hash: str, status: str) -> None:
        self.db.mark_backfill_url(self.bf_id, url_hash, status)
        if status == "SAVED":
            self.saved += 1
        elif status == "SKIPPED":
            self.skipped += 1
        elif status == "OUT_OF_RANGE":
            self.out += 1
        else:
            self.failed += 1

    def _seen_day(self, day: date | None) -> None:
        """날짜 없는 목록: 그 쪽 글을 열어 본 가장 늦은 발행일을 남긴다 (다음 쪽으로 갈지 정한다)."""
        c = self.cursor
        if day is None or c.get("phase") != "LIST" or c.get("dated", True):
            return
        if not c.get("newest") or day.isoformat() > c["newest"]:
            c["newest"] = day.isoformat()

    def _add(self, rows: list[tuple[str, str | None, str | None]]) -> int:
        inserted, _ = self.db.add_backfill_urls(self.bf_id, rows)
        self.found += inserted
        return inserted

    # -------------------------------------------------------------- 주소 찾기
    def _discover_step(self) -> None:
        phase = self.cursor.get("phase")
        if phase is None:
            self._plan()
        elif phase == "SITEMAP":
            self._sitemap_step()
        elif phase == "LIST":
            self._list_step()
        else:
            self.cursor["phase"] = "DONE"

    def _hubs(self) -> list[str]:
        return list(dict.fromkeys([h for k in self.kinds for h in k["hubs"]] + list(self.config.get("hubs") or [])))

    def _plan(self) -> None:
        roots = [u for u in self.f.sitemaps(self.source["base_url"]) if same_site(u, self.domain)]
        if self.config.get("method") == "SITEMAP":
            roots += self._hubs()
        roots = list(dict.fromkeys(roots))
        if not roots:
            self._to_list("robots.txt와 설정에 사이트맵이 없습니다.")
            return
        self.method = "SITEMAP"
        self.cursor.update({"phase": "SITEMAP", "queue": [{"url": u, "depth": 0} for u in roots], "seen": [],
                            "opened": 0, "files": 0, "found": 0, "oldest": None})
        self._note(f"사이트맵 {len(roots)}곳에서 출발합니다.")

    def _sitemap_step(self) -> None:
        c = self.cursor
        queue = c.get("queue") or []
        if not queue or c.get("opened", 0) >= self.s.backfill_max_sitemaps:
            if queue:
                self._note(f"사이트맵 상한({self.s.backfill_max_sitemaps}개)에 닿아 {len(queue)}개는 열지 않았습니다.")
            self._after_sitemaps()
            return
        item = queue.pop(0)
        c["queue"] = queue
        c["opened"] = c.get("opened", 0) + 1
        c["seen"] = (list(c.get("seen") or []) + [item["url"]])[-300:]
        try:
            page = self.f.get_sitemap(item["url"])
        except CrawlFailure as e:
            if e.blocking:
                raise
            self._note(f"사이트맵을 열지 못함 {item['url']}: {e.code.value}")
            return
        if not looks_like_sitemap(page.text[:3000]):
            self._note(f"사이트맵이 아님 {item['url']}")
            return
        is_index, entries = parse_sitemap(page.text)
        if is_index:
            if item["depth"] >= MAX_SITEMAP_DEPTH:
                return
            seen = set(c["seen"]) | {q["url"] for q in queue}
            kids = [e for e in entries if same_site(e.loc, self.domain) and e.loc not in seen
                    and child_wanted(e, self.date_from, self.date_to)]
            kids.sort(key=child_order, reverse=True)
            c["queue"] = [{"url": e.loc, "depth": item["depth"] + 1} for e in kids] + queue
            self._note(f"색인 {short(item['url'])}: 하위 {len(entries)}개 중 기간에 걸치는 {len(kids)}개")
            return
        rows: list[tuple[str, str | None, str | None]] = []
        oldest = date.fromisoformat(c["oldest"]) if c.get("oldest") else None
        for e in entries:
            hit = match_article(e.loc, self.kinds, self.domain)
            if hit is None:
                continue
            day = entry_day(e)
            if day and (oldest is None or day < oldest):
                oldest = day
            if entry_in_period(e, self.date_from, self.date_to):
                rows.append((hit[0], e.published, hit[1]))
        c["oldest"] = oldest.isoformat() if oldest else None
        added = self._add(rows)
        c["files"] = c.get("files", 0) + 1
        c["found"] = c.get("found", 0) + added
        self._note(f"사이트맵 {short(item['url'])}: 항목 {len(entries)}개 중 기간 안 기사 {len(rows)}건"
                   + (" (일부만 읽음: 크기 상한)" if page.truncated else ""))

    def _after_sitemaps(self) -> None:
        """사이트맵을 다 봤다: 기간을 덮었으면 끝, 아니면 목록 페이지로 이어 찾는다."""
        c = self.cursor
        oldest = date.fromisoformat(c["oldest"]) if c.get("oldest") else None
        if not c.get("found"):
            self._to_list("사이트맵에 기간 안 기사가 없어 목록 페이지를 거꾸로 넘깁니다.")
        elif oldest is None:
            self._to_list("사이트맵 항목에 날짜가 없어 목록 페이지로도 찾아 봅니다.")
        elif oldest > self.date_from + timedelta(days=COVERAGE_SLACK_DAYS):
            self._to_list(f"사이트맵이 {oldest}까지만 있어 그 전은 목록 페이지로 이어 찾습니다.")
        else:
            c["phase"] = "DONE"

    def _to_list(self, reason: str) -> None:
        hubs = self._hubs()
        if self.config.get("method") != "STATIC":
            # 피드·사이트맵 소스: 허브가 목록 페이지가 아니므로 탐색 때 본 섹션(목록) 페이지도 넘겨 본다
            excluded = set(self.config.get("excluded_sections") or [])
            hubs += [s["url"] for s in (self.config.get("sections") or []) if s.get("name") not in excluded][:8]
        hubs = list(dict.fromkeys(hubs))
        self._note(reason)
        if not hubs:
            self.cursor["phase"] = "DONE"
            self._note("넘겨 볼 목록 페이지가 없습니다.")
            return
        self.method = "LIST"
        self.cursor.update({"phase": "LIST", "hubs": hubs, "hub": 0})
        self._reset_hub()

    def _reset_hub(self) -> None:
        self.cursor.update({"page": 0, "param": None, "prev": [], "dated": True, "newest": None})

    def _next_hub(self, reason: str) -> None:
        c = self.cursor
        hubs = c.get("hubs") or []
        hub = hubs[c["hub"]] if c.get("hub", 0) < len(hubs) else ""
        self._note(f"목록 {short(hub)}: {reason}")
        c["hub"] = c.get("hub", 0) + 1
        self._reset_hub()

    def _list_step(self) -> None:
        c = self.cursor
        hubs = c.get("hubs") or []
        if c.get("hub", 0) >= len(hubs):
            c["phase"] = "DONE"
            return
        hub = hubs[c["hub"]]
        page_no = c.get("page", 0) + 1
        if page_no > 1 and not c.get("dated", True) and c.get("newest") and \
                date.fromisoformat(c["newest"]) < self.date_from:
            self._next_hub(f"{page_no - 1}쪽 글이 기간 시작보다 이르러 멈춤")
            return
        if page_no > 1 and not c.get("param"):
            self._next_hub("쪽 번호를 넘기는 방법을 찾지 못함")
            return
        if page_no > self.s.backfill_max_pages:
            self._next_hub(f"쪽 수 상한({self.s.backfill_max_pages}쪽)에 닿음")
            return
        url = hub if page_no == 1 else page_url(hub, c["param"], page_no)
        try:
            page = self.f.get(url)
        except CrawlFailure as e:
            if e.blocking:
                raise
            self._next_hub(f"{page_no}쪽을 열지 못함 ({e.code.value})")
            return
        c["page"] = page_no
        if page_no == 1:
            if looks_like_feed(page.text) or looks_like_sitemap(page.text):
                self._next_hub("피드·사이트맵이라 쪽을 넘길 수 없음")
                return
            id_keys = {key for k in self.kinds for key in (k.get("query_keys") or [])}
            c["param"] = find_page_param(page.text, page.url, id_keys)
        entries: list[tuple[str, str | None, str | None]] = []
        seen: set[str] = set()
        for u, hint in links_with_dates(page.text, page.url):
            hit = match_article(u, self.kinds, self.domain)
            if hit is None or hit[0] in seen:
                continue
            seen.add(hit[0])
            entries.append((hit[0], hint, hit[1]))
        prev = set(c.get("prev") or [])
        fresh = [e for e in entries if sha256(e[0])[:16] not in prev]
        c["prev"] = [sha256(e[0])[:16] for e in entries][:300]
        if page_no > 1 and not fresh:
            self._next_hub(f"{page_no}쪽에 새 글이 없어 목록 끝으로 봄")
            return
        days = [kst_day(hint) for _, hint, _ in fresh]
        dated = [d for d in days if d]
        if dated:
            c["dated"] = True
            if max(dated) < self.date_from:
                self._next_hub(f"{page_no}쪽 글이 모두 {self.date_from}보다 일러 멈춤")
                return
            rows = [e for e, d in zip(fresh, days) if d is None or self.date_from <= d <= self.date_to]
        else:
            # 목록에 날짜가 없다: 이미 있는 문서의 발행일과, 이 쪽 글을 열어 본 발행일(_seen_day)로 다음 쪽을 정한다
            c["dated"] = False
            newest = self.db.newest_published([u for u, _, _ in fresh])
            c["newest"] = newest.astimezone(KST).date().isoformat() if newest else None
            rows = fresh
        self._add(rows)


def short(url: str) -> str:
    parts = urlsplit(url)
    tail = parts.path.rsplit("/", 1)[-1] or parts.path
    return (tail + ("?" + parts.query if parts.query else ""))[:80] or url[:80]


def done_message(row: dict, cursor: dict) -> str:
    text = (f"완료: 기사 주소 {row.get('found_count', 0)}건 중 새로 저장 {row.get('saved_count', 0)}건, "
            f"이미 있음 {row.get('skipped_count', 0)}건, 기간 밖·발행일 없음 {row.get('out_of_range_count', 0)}건, "
            f"실패 {row.get('failed_count', 0)}건.")
    notes = cursor.get("notes") or []
    if notes:
        text += " " + notes[-1]
    return text[:500]
