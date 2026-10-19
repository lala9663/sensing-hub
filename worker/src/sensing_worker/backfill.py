"""과거 기사 채우기(BACKFILL): 등록 전에 나온 기사를 기간을 정해 기간 묶음 단위로, 가장 최근 묶음부터 과거로 천천히 가져온다.

평소 수집은 허브에 지금 보이는 최신 기사만 본다 (RSS 50건 안팎, 뉴스 사이트맵 이틀 안팎). 채우기는 그 이전 기사를 찾는다.

기간 묶음
- 요청 기간을 묶음으로 나눈다. 기본 2개월이고, 보관 사이트맵이 한 달·두 달·석 달 단위 파일이면 그 단위에 맞춘다(동아일보 두 달 파일
  하나 = 묶음 하나, 하루 단위 파일이면 한 달 묶음). 연도별처럼 더 긴 파일은 기본 묶음으로 나누고 한 파일을 여러 묶음이 함께 쓴다.
  시작 날짜는 묶음의 첫날로 맞춘다. 묶음은 backfill_segment에 상태(대기·진행·완료·실패)와 수를 남기고, 끊기면 남은 묶음부터 잇는다.
- 요청 기간을 뒤로 늘리면(두 달 더) 다음 묶음에서 더 오래된 묶음을 만든다. 전에 읽은 파일 중 늘어난 기간의 글이 있을 것은 다시 읽는다.

찾는 방법 (묶음마다, 앞에서 덮으면 뒤로 가지 않는다)
1. SITEMAP: robots.txt의 Sitemap 줄(과 사이트맵 허브)에서 출발해 색인의 하위 파일을 backfill_sitemap에 모두 적어 두고, 묶음에 필요한
   파일만 읽는다. 파일 이름에 날짜가 있으면(donga-sitemap-202607-202608.xml.gz, 1993.xml, weekly-sitemap-2000-2005.xml.gz, 20261019.xml)
   그 기간이 묶음에 걸칠 때만 읽는다. 번호만 다른 파일 묶음(mbn_all_sitemap0~549.xml)은 번호가 날짜 순서라고 보고, 양 끝을 읽어 방향을
   알아낸 뒤 이분 탐색으로 묶음에 걸치는 파일만 읽는다 (읽은 파일의 글 날짜 범위가 이웃 파일의 범위를 가둔다). 나머지 이름에 날짜가
   없는 파일은 색인의 lastmod가 묶음 시작 이후일 때만, 채우기 하나에 settings.backfill_max_sitemaps개까지 읽는다.
   글의 날짜는 주소 안 날짜(/20260915/) → news:publication_date → (파일 기간 안으로 자른) lastmod 순서로 본다. lastmod는 나중에 고친
   날이 섞이므로(1996 파일에 2024 lastmod) "그보다 이른 글"이라는 뜻으로만 쓰고, 상세의 발행일로 다시 확인해 맞는 묶음으로 옮긴다.
2. LIST: 사이트맵이 묶음을 덮지 못하면(날짜 있는 파일이 없고, 날짜 없는 파일의 가장 이른 글이 묶음 시작보다 늦으면) 목록 페이지
   (HTML 허브)를 쪽 번호로 거꾸로 넘긴다. 쪽은 묶음 사이에 이어서 넘긴다(다음 묶음은 멈춘 쪽부터). 앞 쪽에 없던 글(고정 공지 제외)의
   날짜가 모두 묶음 시작보다 이르면 그 묶음은 끝, 요청 기간 시작보다 이르면 그 허브는 끝이다. 목록에 날짜가 없으면 열어 본 발행일로 판단한다.

기사 판정·추출·저장·키워드 대조는 평소 수집(jobs.JobRunner._crawl)과 같은 함수를 쓴다 — 기사 패턴(article_pattern 또는 기사 종류
article_patterns)·matches·canonical_article_url, extract_article(설정 selectors·date_format), upsert_document(언론사는 제목·링크·
발행일만, 본문 미저장), _match_watches, 첨부. 열어 본 발행일이 요청 기간 밖이거나 없으면 저장하지 않는다(OUT_OF_RANGE).

저속·저우선
- 한 묶음 작업(crawl_job BACKFILL 하나)에 요청 settings.backfill_chunk(기본 100)건까지 하고 진행 위치를 남긴다: cursor(단계·목록 쪽 번호),
  backfill_sitemap(읽은 파일), backfill_url(찾은 주소별 상태). 다음 작업은 큐가 빈 워커가 넣는다(main.backfill_idle).
- 작업 도중에도 기사마다 확인해 평소 작업(CRAWL·ONBOARD)이 대기하거나 이 소스의 주기 수집 차례가 오면 멈추고 양보한다. 취소도 같다.
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
from collections import Counter, defaultdict
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
# 날짜 없는 사이트맵의 가장 이른 글이 묶음 시작보다 이만큼 넘게 늦으면 사이트맵이 묶음을 덮지 못한 것으로 보고 목록 페이지로 찾는다
COVERAGE_SLACK_DAYS = 3
MAX_SITEMAP_DEPTH = 3      # 색인 → 색인 → 사이트맵
NUMBERED_MIN = 10          # 번호만 다른 하위 파일이 이만큼 넘으면 번호 순서를 날짜 순서로 보고 이분 탐색한다
ARTICLE_BATCH = 10         # 한 번에 꺼내 여는 주소 수 (주소마다 양보·취소를 확인한다)
RECORDED_ERRORS = 3        # 작업 하나에서 crawl_error로 남기는 기사 실패 수 (나머지는 수만 센다)
MAX_NOTES = 8
CANCELLED = "취소됨"        # 작업을 멈춘 이유: 사용자가 취소 (나머지 이유는 양보·상한)
READ_ALL = date.min        # 색인은 한 번 읽으면 하위가 모두 들어온다 (다시 읽지 않는다)


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


# 기사 주소 경로 안의 날짜: /20260915/ (동아일보), /2026/09/15/, /2026-09-15-slug
_URL_DATE_RES = (
    re.compile(r"/((?:19|20)\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?=/|$|[._-][^\d])"),
    re.compile(r"/((?:19|20)\d{2})/(0[1-9]|1[0-2])/(0[1-9]|[12]\d|3[01])(?=/|$|[._-])"),
    re.compile(r"/((?:19|20)\d{2})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])(?=/|$|[._-])"),
)


def url_day(url: str, today: date | None = None) -> date | None:
    """기사 주소 경로에 든 날짜 (경로 조각 하나가 날짜일 때만, 쿼리는 보지 않는다). 없거나 미래면 None."""
    path = urlsplit(url).path
    limit = (today or datetime.now(KST).date()) + timedelta(days=1)
    for regex in _URL_DATE_RES:
        m = regex.search(path)
        if m:
            try:
                d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                continue
            if d <= limit:
                return d
    return None


# 파일 이름의 날짜 조각: 2026 | 202609 | 2026-09 | 20260915 | 2026/09/15
_SPAN_TOKEN_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?:[-_./]?(0[1-9]|1[0-2])(?:[-_./]?(0[1-9]|[12]\d|3[01]))?)?(?!\d)")


def sitemap_span(url: str, today: date | None = None) -> tuple[date, date] | None:
    """사이트맵 주소(경로·쿼리)의 날짜로 본 기간. donga-sitemap-202609-202610.xml.gz → (2026-09-01, 2026-10-31).
    날짜 조각이 둘 이상이면 처음 것의 시작부터 마지막 것의 끝까지(weekly-sitemap-2000-2005 → 2000-01-01~2005-12-31).
    연도만 있는 조각은 올해 이하일 때만 연도로 본다 (sitemap-2048.xml 같은 쪽 번호를 연도로 읽지 않게). 날짜가 없으면 None."""
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


_LAST_NUMBER_RE = re.compile(r"(\d+)(?=\D*$)")


def numbered(url: str) -> tuple[str, int] | None:
    """번호만 다른 파일의 묶음 이름과 번호: .../mbn_all_sitemap12.xml → (".../mbn_all_sitemap{n}.xml", 12)."""
    parts = urlsplit(url)
    m = _LAST_NUMBER_RE.search(parts.path)
    if not m:
        return None
    return f"{parts.netloc}{parts.path[:m.start()]}{{n}}{parts.path[m.end():]}", int(m.group(1))


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


def resolve_entry(entry: SitemapEntry, span: tuple[date, date] | None) -> tuple[date | None, bool]:
    """사이트맵 항목의 날짜와 믿을 만한지. 주소 안 날짜 → news:publication_date (믿을 만함) → lastmod (나중에 고친 날일 수 있어
    "그보다 이른 글"이라는 뜻: 믿을 만하지 않음. 파일 이름에 기간이 있으면 그 안으로 자르고, lastmod가 없으면 파일 기간의 끝)."""
    d = url_day(entry.loc) or kst_day(entry.published)
    if d:
        return d, True
    m = kst_day(entry.lastmod)
    if span:
        return (span[1] if m is None else min(max(m, span[0]), span[1])), False
    return m, False


def sitemap_child_rows(entries: list[SitemapEntry], depth: int, today: date | None = None) -> list[dict]:
    """색인의 하위 파일을 backfill_sitemap 행으로. 이름에 날짜가 없고 번호만 다른 파일이 NUMBERED_MIN개를 넘으면 묶음(grp·num)으로 적는다."""
    rows = []
    for e in entries:
        span = sitemap_span(e.loc, today)
        rows.append({"url": e.loc, "depth": depth, "span_from": span[0] if span else None,
                     "span_to": span[1] if span else None, "lastmod": kst_day(e.lastmod), "grp": None, "num": None})
    groups: dict[str, list[tuple[dict, int]]] = defaultdict(list)
    for r in rows:
        if r["span_from"] is None:
            key = numbered(r["url"])
            if key:
                groups[key[0]].append((r, key[1]))
    for key, members in groups.items():
        if len(members) >= NUMBERED_MIN:
            for r, n in members:
                r["grp"], r["num"] = key, n
    return rows


def _overlaps(a_from: date | None, a_to: date | None, b_from: date, b_to: date) -> bool:
    return (a_from is None or a_from <= b_to) and (a_to is None or a_to >= b_from)


def _needs_read(r: dict, seg_from: date, seg_to: date) -> bool:
    """이 묶음을 위해 (다시) 읽어야 하는지: 아직 안 읽었거나, 이 묶음 시작보다 늦은 날짜까지만 나눠 넣었고 그보다 이른 글이 든 파일."""
    if r["read_from"] is None:
        return True
    return r["read_from"] > seg_from and (r["obs_from"] is None or r["obs_from"] <= seg_to)


def pick_numbered(rows: list[dict], seg_from: date, seg_to: date) -> dict | None:
    """번호 묶음에서 다음에 읽을 파일. 양 끝을 먼저 읽어 번호가 커질수록 최근인지 오래된지 알아낸 뒤, 읽은 파일의 글 날짜 범위로
    안 읽은 파일의 범위를 가두고(번호 순서 = 날짜 순서), 묶음에 걸칠 수 있는 파일들의 가운데를 읽는다 (이분 탐색)."""
    rows = sorted(rows, key=lambda r: r["num"])
    for end in (rows[0], rows[-1]):
        if end["read_at"] is None:
            return end
    def mid(r):
        return r["obs_to"] or r["obs_from"]
    lo_row, hi_row = rows[0], rows[-1]
    newest_first = bool(mid(lo_row) and mid(hi_row) and mid(lo_row) >= mid(hi_row))
    ordered = rows if newest_first else rows[::-1]         # 최근 파일부터
    known = [i for i, r in enumerate(ordered) if r["read_at"] is not None and not r["error"] and r["obs_from"]]
    candidates: list[int] = []
    for i, r in enumerate(ordered):
        if r["error"]:
            continue
        if r["read_at"] is not None:
            if r["obs_from"] and _needs_read(r, seg_from, seg_to) and _overlaps(r["obs_from"], r["obs_to"], seg_from, seg_to):
                return r                                    # 기간을 늘려 다시 읽을 파일
            continue
        if r["lastmod"] is not None and r["lastmod"] < seg_from:
            continue
        newer = [k for k in known if k < i]
        older = [k for k in known if k > i]
        hi = ordered[newer[-1]]["obs_from"] if newer else None   # 더 최근 파일의 가장 이른 글보다 늦을 수 없다
        lo = ordered[older[0]]["obs_to"] if older else None      # 더 오래된 파일의 가장 늦은 글보다 이를 수 없다
        if _overlaps(lo, hi, seg_from, seg_to):
            candidates.append(i)
    if not candidates:
        return None
    return ordered[candidates[len(candidates) // 2]]


def pick_sitemap(rows: list[dict], seg_from: date, seg_to: date, plain_cap_reached: bool) -> dict | None:
    """묶음 [seg_from, seg_to]를 위해 다음에 읽을 사이트맵 파일. 이름에 날짜가 있는 파일(최근 것부터) → 번호 묶음(이분 탐색) →
    날짜 없는 나머지 파일(lastmod 최근 것부터, 상한까지). 색인은 한 번만 읽는다."""
    dated, plain = [], []
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r["error"] or (r["is_index"] and r["read_from"] is not None):
            continue
        if r["grp"]:
            groups[r["grp"]].append(r)
            continue
        if not _needs_read(r, seg_from, seg_to):
            continue
        if r["span_from"] is not None:
            if _overlaps(r["span_from"], r["span_to"], seg_from, seg_to):
                dated.append(r)
        elif r["lastmod"] is None or r["lastmod"] >= seg_from:
            plain.append(r)
    if dated:
        return max(dated, key=lambda r: (r["span_to"], r["span_from"]))
    for members in groups.values():
        pick = pick_numbered(members, seg_from, seg_to)
        if pick:
            return pick
    if plain and not plain_cap_reached:
        return max(plain, key=lambda r: (r["read_at"] is None, r["lastmod"] or date.min))
    return None


def sitemaps_cover(rows: list[dict], seg_from: date, seg_to: date) -> bool:
    """사이트맵만으로 묶음을 덮는지: 이름에 날짜가 있고 묶음에 걸치는 파일을 읽었거나, 날짜 없는 파일의 글이 묶음 시작(+여유)까지 닿았다."""
    reached: date | None = None
    for r in rows:
        if r["is_index"] or r["read_at"] is None or r["error"]:
            continue
        if r["span_from"] is not None and _overlaps(r["span_from"], r["span_to"], seg_from, seg_to):
            return True
        if r["span_from"] is None and r["obs_from"] and r["obs_to"] and r["obs_to"] >= seg_from:
            reached = r["obs_from"] if reached is None else min(reached, r["obs_from"])
    return reached is not None and reached <= seg_from + timedelta(days=COVERAGE_SLACK_DAYS)


# ---------------------------------------------------------------------- 기간 묶음

def month_index(d: date) -> int:
    return d.year * 12 + d.month - 1


def month_start(index: int) -> date:
    return date(index // 12, index % 12 + 1, 1)


def segment_start(d: date, unit: int, offset: int) -> date:
    """d가 든 묶음의 첫날. 묶음은 month_index % unit == offset인 달에서 시작한다 (2/0: 1~2월, 3~4월 …)."""
    i = month_index(d)
    return month_start(i - ((i - offset) % unit))


def plan_segments(date_from: date, date_to: date, unit: int, offset: int) -> list[tuple[date, date]]:
    """[date_from, date_to]를 묶음으로, 가장 최근 것부터. 가장 최근 묶음은 date_to에서 끝나고, 가장 오래된 묶음은 date_from에서 시작한다."""
    out: list[tuple[date, date]] = []
    end = date_to
    while end >= date_from:
        start = segment_start(end, unit, offset)
        out.append((max(start, date_from), end))
        end = start - timedelta(days=1)
    return out


def detect_unit(spans: list[tuple[date, date]], default_unit: int = 2, default_offset: int = 0) -> tuple[int, int]:
    """보관 사이트맵 파일 이름의 기간으로 묶음 단위(개월)와 시작 달을 정한다. 가장 흔한 파일 길이가 1~3개월이면 그 단위(하루 파일은
    한 달), 더 길면(연도별·5년 단위) 기본 단위로 나눠 한 파일을 여러 묶음이 함께 읽는다. 파일이 없으면 기본 단위."""
    lengths = Counter(month_index(e) - month_index(s) + 1 for s, e in spans)
    if not lengths:
        return default_unit, default_offset
    unit = lengths.most_common(1)[0][0]
    if unit not in (1, 2, 3):
        return default_unit, default_offset
    offsets = Counter(month_index(s) % unit for s, e in spans if month_index(e) - month_index(s) + 1 == unit)
    return unit, offsets.most_common(1)[0][0]


def segment_label(start: date, end: date) -> str:
    """2026-09~10, 2026-09, 2025-11~2026-01"""
    if (start.year, start.month) == (end.year, end.month):
        return f"{start:%Y-%m}"
    if start.year == end.year:
        return f"{start:%Y-%m}~{end:%m}"
    return f"{start:%Y-%m}~{end:%Y-%m}"


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


# ---------------------------------------------------------------------- 작업 실행

def run_backfill(runner, job: Job, source: dict) -> str:
    """BACKFILL 작업 하나를 실행한다. 작업 상태를 돌려준다."""
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
        self.unit: int = bf["unit_months"]
        self.offset: int = bf["unit_offset"]
        self.cursor: dict = dict(bf["cursor"] or {})
        if self.cursor.get("v") != 2:          # 기간 묶음 이전 형식의 진행 위치는 처음부터 다시 (찾은 주소는 남아 있다)
            self.cursor = {"v": 2}
        self.kinds = article_kinds(config)
        self.named = len(config.get("article_patterns") or []) > 1
        self.domain = config.get("site_domain") or source["domain"]
        self.start_requests = self.f.request_count
        self.found = self.saved = self.skipped = self.out = self.failed = 0
        self.recorded = 0
        self.segments: list[dict] = []
        self.current: dict | None = None

    # -------------------------------------------------------------- 흐름
    def run(self) -> str:
        stop: str | None = None
        done = False
        try:
            if self.cursor.get("phase") == "SEGMENTS":
                self._load_segments()
            while True:
                stop = self._stop_reason()
                if stop:
                    break
                if self.cursor.get("phase") != "SEGMENTS":
                    self._plan_step()
                    continue
                seg = self._current_segment()
                self.current = seg
                rows = self.db.next_backfill_urls(self.bf_id, seg["seq"] if seg else 10 ** 9, ARTICLE_BATCH)
                if rows:
                    for row in rows:
                        stop = self._stop_reason()
                        if stop:
                            break
                        self._article(row)
                    if stop:
                        break
                    continue
                if seg is None:
                    done = True
                    break
                if seg["status"] == "QUEUED":
                    self.db.start_segment(seg["id"])
                    seg["status"] = "RUNNING"
                if not self._segment_step(seg):
                    self._finish_segment(seg)
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
        self.db.save_backfill(self.bf_id, method=self._method(), cursor=self.cursor, requests=self._used(), message=message)
        if done and pending == 0:
            row = self.db.load_backfill(self.job.id, self.job.source_id) or {}
            self.db.finish_backfill(self.bf_id, "DONE", done_message(row, self.db.load_segments(self.bf_id)))
        status = "PARTIAL" if self.failed else "SUCCESS"
        where = f" {segment_label(self.current['date_from'], self.current['date_to'])}" if self.current else ""
        note = f"과거 기사 채우기 #{self.bf_id}{where}: 새 {self.saved} · 이미 {self.skipped} · 남은 {pending}"
        if stop and stop != "BUDGET":
            note += f" · {stop}"
        self.db.finish_job(self.job.id, status, self.found, self.saved, self.failed,
                           self.source.get("active_config_version_id"), note=note)
        log.info("backfill %s source=%s chunk: requests=%s found=%s saved=%s skipped=%s out=%s failed=%s pending=%s "
                 "segment=%s stop=%s", self.bf_id, self.job.source_id, self._used(), self.found, self.saved, self.skipped,
                 self.out, self.failed, pending, where.strip() or self.cursor.get("phase"), stop)
        return status

    def _blocked(self, e: CrawlFailure) -> str:
        """차단 신호: 평소 수집과 같이 우회하지 않고 멈추고 소스를 BLOCKED로 둔다 (관리자 재점검으로만 다시)."""
        self.r._record(self.job, e)
        if e.blocking:
            self.db.set_source_status(self.job.source_id, "BLOCKED", e.code.value)
            message = f"사이트가 자동 접근을 막아 멈췄습니다 ({e.code.value}: {e}). 소스는 차단(BLOCKED)으로 두었습니다."
        else:
            message = f"멈췄습니다 ({e.code.value}: {e})."
        if self.current is not None:
            self.db.finish_segment(self.current["id"], "FAILED", self._segment_method(self.current), message)
        self.db.save_backfill(self.bf_id, method=self._method(), cursor=self.cursor, requests=self._used(), message=None)
        self.db.finish_backfill(self.bf_id, "FAILED", message)
        self.db.finish_job(self.job.id, "FAILED", self.found, self.saved, self.failed + 1,
                           self.source.get("active_config_version_id"), note=f"과거 기사 채우기 #{self.bf_id} 중단: {e.code.value}")
        log.warning("backfill %s source=%s stopped: %s %s", self.bf_id, self.job.source_id, e.code.value, e)
        return "FAILED"

    def _progress(self, stop: str | None) -> str:
        if self.cursor.get("phase") != "SEGMENTS":
            where = "사이트맵 색인을 읽고 기간 묶음을 나누는 중"
        elif self.current is not None:
            where = f"{segment_label(self.current['date_from'], self.current['date_to'])} 묶음 진행 중"
        else:
            where = "남은 기사를 여는 중"
        if stop and stop != "BUDGET":
            where += f" · 잠시 멈춤: {stop}"
        return where

    def _method(self) -> str | None:
        states = (self.cursor.get("segs") or {}).values()
        if any(st.get("list_needed") for st in states):
            return "LIST" if not any(st.get("files") for st in states) else "SITEMAP"
        return "SITEMAP" if self.cursor.get("phase") else None

    def _note(self, text: str) -> None:
        notes = list(self.cursor.get("notes") or [])
        notes.append(text[:200])
        self.cursor["notes"] = notes[-MAX_NOTES:]

    # -------------------------------------------------------------- 기간 묶음
    def _load_segments(self) -> None:
        self.segments = self.db.load_segments(self.bf_id)
        self._extend_if_needed()

    def _extend_if_needed(self) -> None:
        """요청 기간을 뒤로 늘렸으면(두 달 더) 더 오래된 묶음을 만들고, 기간 시작에 닿아 멈췄던 목록 허브를 다시 연다."""
        oldest = min((s["date_from"] for s in self.segments), default=None)
        if oldest is None or self.date_from >= oldest:
            return
        start_seq = max(s["seq"] for s in self.segments) + 1
        new = plan_segments(self.date_from, oldest - timedelta(days=1), self.unit, self.offset)
        self.db.add_segments(self.bf_id, [(start_seq + i, a, b) for i, (a, b) in enumerate(new)])
        for h in (self.cursor.get("list") or {}).get("hubs") or []:
            if h.get("stop") == "older":
                h.update({"done": False, "stop": None, "page": max(h["page"] - 1, 0), "prev": []})
        self.segments = self.db.load_segments(self.bf_id)
        # 전에 열어 보니 기간 밖(늘린 기간 안)이었던 글은 다시 연다 — 열어 본 발행일을 date_hint에 남겨 두었다
        moves = []
        for r in self.db.out_of_range_urls(self.bf_id):
            day = kst_day(r["date_hint"])
            target = self._segment_of(day) if day and self.date_from <= day < oldest else None
            if target is not None:
                moves.append((r["url_hash"], r["segment_id"], target["id"]))
        self.db.reopen_backfill_urls(self.bf_id, moves)
        self._note(f"기간을 {self.date_from}까지 늘려 묶음 {len(new)}개를 더했습니다"
                   + (f" (전에 기간 밖이던 글 {len(moves)}건을 다시 엽니다)." if moves else "."))

    def _current_segment(self) -> dict | None:
        return next((s for s in self.segments if s["status"] in ("QUEUED", "RUNNING")), None)

    def _segment_of(self, day: date) -> dict | None:
        return next((s for s in self.segments if s["date_from"] <= day <= s["date_to"]), None)

    def _state(self, seg: dict) -> dict:
        return self.cursor.setdefault("segs", {}).setdefault(
            str(seg["id"]), {"sm_done": False, "list_needed": False, "list_done": False, "files": 0, "pages": 0})

    def _segment_method(self, seg: dict) -> str | None:
        st = self._state(seg)
        parts = (["SITEMAP"] if st["files"] else []) + (["LIST"] if st["list_needed"] and st["pages"] else [])
        return "+".join(parts) or None

    def _segment_step(self, seg: dict) -> bool:
        """이 묶음의 주소 찾기를 한 걸음. 더 할 것이 없으면 False."""
        st = self._state(seg)
        if not st["sm_done"]:
            rows = self.db.load_sitemaps(self.bf_id)
            pick = pick_sitemap(rows, seg["date_from"], seg["date_to"],
                                self.cursor.get("plain_reads", 0) >= self.s.backfill_max_sitemaps)
            if pick is not None:
                self._read_sitemap(pick)
                st["files"] += 1
                return True
            st["sm_done"] = True
            if not sitemaps_cover(rows, seg["date_from"], seg["date_to"]):
                st["list_needed"] = True
                self._init_list()
            return True
        if st["list_needed"] and not st["list_done"]:
            hub = self._list_hub(seg)
            if hub is None:
                st["list_done"] = True
            else:
                self._list_page(seg, hub)
                st["pages"] += 1
            return True
        return False

    def _finish_segment(self, seg: dict) -> None:
        st = self._state(seg)
        label = segment_label(seg["date_from"], seg["date_to"])
        row = next((s for s in self.db.load_segments(self.bf_id) if s["id"] == seg["id"]), seg)
        hubs = (self.cursor.get("list") or {}).get("hubs") or []
        no_way = st["list_needed"] and not st["files"] and not any(h.get("param") for h in hubs)
        if no_way and not row["found_count"]:
            status, message = "FAILED", f"{label}: 보관 사이트맵도 넘길 목록 페이지도 없어 채우지 못했습니다."
        else:
            how = ([f"사이트맵 {st['files']}개"] if st["files"] else []) + ([f"목록 {st['pages']}쪽"] if st["pages"] else [])
            status, message = "DONE", f"{label} 완료 ({', '.join(how) or '찾을 곳 없음'})"
        self.db.finish_segment(seg["id"], status, self._segment_method(seg), message)
        seg["status"] = status
        self._note(message)

    # -------------------------------------------------------------- 계획: 사이트맵 색인 읽기 → 단위 정하기 → 묶음 나누기
    def _hubs(self) -> list[str]:
        return list(dict.fromkeys([h for k in self.kinds for h in k["hubs"]] + list(self.config.get("hubs") or [])))

    def _plan_step(self) -> None:
        phase = self.cursor.get("phase")
        if phase is None:
            roots = [u for u in self.f.sitemaps(self.source["base_url"]) if same_site(u, self.domain)]
            if self.config.get("method") == "SITEMAP":
                roots += self._hubs()
            roots = list(dict.fromkeys(roots))
            self.db.add_sitemaps(self.bf_id, [{**r, "depth": 0} for r in sitemap_child_rows(
                [SitemapEntry(u) for u in roots], 0)])
            self.cursor["phase"] = "CLASSIFY"
            self._note(f"사이트맵 {len(roots)}곳에서 출발합니다." if roots else "robots.txt와 설정에 사이트맵이 없습니다.")
            return
        rows = self.db.load_sitemaps(self.bf_id)
        unread = [r for r in rows if r["depth"] == 0 and r["read_at"] is None]
        if unread:
            self._read_sitemap(unread[0], classify=True)
            return
        spans = [(r["span_from"], r["span_to"]) for r in rows if r["depth"] > 0 and r["span_from"] is not None]
        self.unit, self.offset = detect_unit(spans, self.unit, self.offset)
        self.date_from = segment_start(self.date_from, self.unit, self.offset)
        self.db.set_backfill_unit(self.bf_id, self.unit, self.offset, self.date_from)
        planned = plan_segments(self.date_from, self.date_to, self.unit, self.offset)
        self.db.add_segments(self.bf_id, [(i, a, b) for i, (a, b) in enumerate(planned)])
        self.cursor["phase"] = "SEGMENTS"
        self._note(f"{self.date_from}~{self.date_to}를 {self.unit}개월 묶음 {len(planned)}개로 나눠 최근 것부터 채웁니다"
                   + (" (보관 사이트맵 단위)" if spans else "") + ".")
        self._load_segments()

    def _read_sitemap(self, row: dict, classify: bool = False) -> None:
        """사이트맵 파일 하나를 읽는다. 색인이면 하위 파일을 적고, 사이트맵이면 기사 주소를 날짜에 맞는 묶음에 나눠 넣는다
        (색인을 읽는 계획 단계에서는 묶음이 아직 없으므로 사이트맵은 표시만 하고 나중에 다시 읽는다)."""
        url = row["url"]
        try:
            page = self.f.get_sitemap(url)
        except CrawlFailure as e:
            if e.blocking:
                raise
            self.db.mark_sitemap(self.bf_id, url, is_index=None, read_from=None, error=e.code.value)
            self._note(f"사이트맵을 열지 못함 {short(url)}: {e.code.value}")
            return
        if not looks_like_sitemap(page.text[:3000]):
            self.db.mark_sitemap(self.bf_id, url, is_index=None, read_from=None, error="NOT_SITEMAP")
            self._note(f"사이트맵이 아님 {short(url)}")
            return
        is_index, entries = parse_sitemap(page.text)
        if is_index:
            kids = [e for e in entries if same_site(e.loc, self.domain)]
            if row["depth"] < MAX_SITEMAP_DEPTH:
                self.db.add_sitemaps(self.bf_id, sitemap_child_rows(kids, row["depth"] + 1))
            self.db.mark_sitemap(self.bf_id, url, is_index=True, read_from=READ_ALL, entries=len(entries))
            dated = sum(1 for e in kids if sitemap_span(e.loc))
            self._note(f"색인 {short(url)}: 하위 {len(kids)}개" + (f" (이름에 날짜 {dated}개)" if dated else ""))
            return
        if classify:
            self.db.mark_sitemap(self.bf_id, url, is_index=False, read_from=None, entries=len(entries))
            return
        span = (row["span_from"], row["span_to"]) if row["span_from"] is not None else None
        rows: list[tuple[str, str | None, str | None, int | None]] = []
        sure, unsure = [], []
        for e in entries:
            day, reliable = resolve_entry(e, span)
            if day:
                (sure if reliable else unsure).append(day)
            hit = match_article(e.loc, self.kinds, self.domain)
            if hit is None:
                continue
            target = self._target(day, reliable)
            if target is None:
                continue
            hint = e.published or (url_day(e.loc).isoformat() if url_day(e.loc) else None)
            rows.append((hit[0], hint, hit[1], target["id"]))
        days = sure or unsure
        self.db.mark_sitemap(self.bf_id, url, is_index=False, read_from=self.date_from,
                             obs_from=min(days) if days else None, obs_to=max(days) if days else None, entries=len(entries))
        if span is None and not row["grp"]:      # 번호 묶음은 이분 탐색이라 상한에 세지 않는다
            self.cursor["plain_reads"] = self.cursor.get("plain_reads", 0) + 1
        added = self._add(rows)
        self._note(f"사이트맵 {short(url)}: 항목 {len(entries)}개 중 기간 안 기사 {len(rows)}건 (새 주소 {added})"
                   + (" (일부만 읽음: 크기 상한)" if page.truncated else ""))

    def _target(self, day: date | None, reliable: bool) -> dict | None:
        """찾은 주소를 넣을 묶음. 날짜를 모르면 지금 묶음, 믿을 만한 날짜면 그 날짜의 묶음(요청 기간 밖이면 버림),
        lastmod뿐이면(실제 발행일은 그보다 이르다) 그 날짜의 묶음(기간 끝보다 늦으면 가장 최근 묶음, 기간 시작보다 이르면 버림)."""
        if day is None:
            return self.current
        if reliable:
            return self._segment_of(day) if self.date_from <= day <= self.date_to else None
        if day < self.date_from:
            return None
        return self._segment_of(min(day, self.date_to))

    def _add(self, rows: list[tuple[str, str | None, str | None, int | None]]) -> int:
        inserted, _ = self.db.add_backfill_urls(self.bf_id, rows)
        self.found += inserted
        return inserted

    # -------------------------------------------------------------- 목록 페이지 역순 (묶음 사이에 이어서)
    def _init_list(self) -> None:
        if "list" in self.cursor:
            return
        hubs = self._hubs()
        if self.config.get("method") != "STATIC":
            # 피드·사이트맵 소스: 허브가 목록 페이지가 아니므로 탐색 때 본 섹션(목록) 페이지도 넘겨 본다
            excluded = set(self.config.get("excluded_sections") or [])
            hubs += [s["url"] for s in (self.config.get("sections") or []) if s.get("name") not in excluded][:8]
        hubs = list(dict.fromkeys(hubs))
        self.cursor["list"] = {"hubs": [{"url": u, "page": 0, "param": None, "prev": [], "dated": True, "newest": None,
                                         "reached": None, "done": False, "stop": None} for u in hubs]}
        self._note("사이트맵이 묶음을 덮지 못해 목록 페이지를 거꾸로 넘깁니다." if hubs else "넘겨 볼 목록 페이지가 없습니다.")

    def _hub_done(self, hub: dict, stop: str, reason: str) -> None:
        hub.update({"done": True, "stop": stop})
        self._note(f"목록 {short(hub['url'])}: {reason}")

    def _list_hub(self, seg: dict) -> dict | None:
        """이 묶음을 위해 더 넘길 허브. 모든 허브가 끝났거나 묶음 시작보다 이른 글까지 갔으면 None."""
        for hub in (self.cursor.get("list") or {}).get("hubs") or []:
            if hub["done"]:
                continue
            if not hub["dated"] and hub["page"] and hub["newest"]:
                # 날짜 없는 목록: 그 쪽 글을 열어 본 가장 늦은 발행일이 이 허브가 닿은 곳이다
                hub["reached"] = hub["newest"]
                if date.fromisoformat(hub["newest"]) < self.date_from:
                    self._hub_done(hub, "older", f"{hub['page']}쪽 글이 기간 시작보다 이르러 멈춤")
                    continue
            if hub["reached"] and date.fromisoformat(hub["reached"]) < seg["date_from"]:
                continue
            return hub
        return None

    def _list_page(self, seg: dict, hub: dict) -> None:
        self.cursor["list"]["active"] = hub["url"]
        page_no = hub["page"] + 1
        if page_no > 1 and not hub["param"]:
            self._hub_done(hub, "no-param", "쪽 번호를 넘기는 방법을 찾지 못함")
            return
        if page_no > self.s.backfill_max_pages:
            self._hub_done(hub, "cap", f"쪽 수 상한({self.s.backfill_max_pages}쪽)에 닿음")
            return
        url = hub["url"] if page_no == 1 else page_url(hub["url"], hub["param"], page_no)
        try:
            page = self.f.get(url)
        except CrawlFailure as e:
            if e.blocking:
                raise
            self._hub_done(hub, "error", f"{page_no}쪽을 열지 못함 ({e.code.value})")
            return
        hub["page"] = page_no
        if page_no == 1:
            if looks_like_feed(page.text) or looks_like_sitemap(page.text):
                self._hub_done(hub, "feed", "피드·사이트맵이라 쪽을 넘길 수 없음")
                return
            id_keys = {key for k in self.kinds for key in (k.get("query_keys") or [])}
            hub["param"] = find_page_param(page.text, page.url, id_keys)
        entries: list[tuple[str, str | None, str | None]] = []
        seen: set[str] = set()
        for u, hint in links_with_dates(page.text, page.url):
            hit = match_article(u, self.kinds, self.domain)
            if hit is None or hit[0] in seen:
                continue
            seen.add(hit[0])
            entries.append((hit[0], hint or (url_day(hit[0]).isoformat() if url_day(hit[0]) else None), hit[1]))
        prev = set(hub["prev"] or [])
        fresh = [e for e in entries if sha256(e[0])[:16] not in prev]
        hub["prev"] = [sha256(e[0])[:16] for e in entries][:300]
        if page_no > 1 and not fresh:
            self._hub_done(hub, "end", f"{page_no}쪽에 새 글이 없어 목록 끝으로 봄")
            return
        days = [kst_day(hint) for _, hint, _ in fresh]
        dated = [d for d in days if d]
        rows: list[tuple[str, str | None, str | None, int | None]] = []
        if dated:
            hub["dated"] = True
            # 이 허브가 닿은 곳 = 쪽 맨 아래 글의 날짜 (목록은 최근 글부터라 위쪽에 고정된 오래된 공지에 끌려가지 않는다)
            hub["reached"] = next(d for d in reversed(days) if d).isoformat()
            if max(dated) < self.date_from:
                self._hub_done(hub, "older", f"{page_no}쪽 글이 모두 {self.date_from}보다 일러 멈춤")
                return
            for (u, hint, kind), d in zip(fresh, days):
                target = self.current if d is None else (self._segment_of(d) if self.date_from <= d <= self.date_to else None)
                if target is not None:
                    rows.append((u, hint, kind, target["id"]))
        else:
            # 목록에 날짜가 없다: 이미 있는 문서의 발행일과, 이 쪽 글을 열어 본 발행일(_seen_day)로 이 허브가 닿은 곳을 정한다
            hub["dated"] = False
            newest = self.db.newest_published([u for u, _, _ in fresh])
            hub["newest"] = newest.astimezone(KST).date().isoformat() if newest else None
            rows = [(u, hint, kind, seg["id"]) for u, hint, kind in fresh]
        self._add(rows)

    def _seen_day(self, day: date | None) -> None:
        """날짜 없는 목록: 그 쪽 글을 열어 본 가장 늦은 발행일을 허브에 남긴다."""
        lst = self.cursor.get("list") or {}
        hub = next((h for h in lst.get("hubs") or [] if h["url"] == lst.get("active")), None)
        if day is None or hub is None or hub["dated"]:
            return
        if not hub["newest"] or day.isoformat() > hub["newest"]:
            hub["newest"] = day.isoformat()

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
            self._mark(url_hash, "OUT_OF_RANGE", published=day.isoformat() if day else None)
            return
        target = self._segment_of(day)
        move = target["id"] if target is not None and target["id"] != row["segment_id"] else None
        now = datetime.now(timezone.utc)
        _, result = store_article(self.r, self.job, self.config, url, art,
                                  section=row["kind"] if self.named else None,
                                  collected_at=min(art.published_at, now))
        self._mark(url_hash, "SAVED" if result == "NEW" else "SKIPPED", move)

    def _mark(self, url_hash: str, status: str, move_to: int | None = None, published: str | None = None) -> None:
        self.db.mark_backfill_url(self.bf_id, url_hash, status, move_to, published)
        if status == "SAVED":
            self.saved += 1
        elif status == "SKIPPED":
            self.skipped += 1
        elif status == "OUT_OF_RANGE":
            self.out += 1
        else:
            self.failed += 1


def short(url: str) -> str:
    parts = urlsplit(url)
    tail = parts.path.rsplit("/", 1)[-1] or parts.path
    return (tail + ("?" + parts.query if parts.query else ""))[:80] or url[:80]


def done_message(row: dict, segments: list[dict]) -> str:
    done = sum(1 for s in segments if s["status"] == "DONE")
    failed = sum(1 for s in segments if s["status"] == "FAILED")
    text = (f"완료: 묶음 {len(segments)}개 중 완료 {done}" + (f"·실패 {failed}" if failed else "")
            + f". 기사 주소 {row.get('found_count', 0)}건 중 새로 저장 {row.get('saved_count', 0)}건, "
            f"이미 있음 {row.get('skipped_count', 0)}건, 기간 밖·발행일 없음 {row.get('out_of_range_count', 0)}건, "
            f"실패 {row.get('failed_count', 0)}건.")
    return text[:500]
