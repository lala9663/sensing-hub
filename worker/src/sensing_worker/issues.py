"""이슈 묶기: 여러 언론사가 같은 사건을 보도한 기사를 하나의 이슈로 묶는다 (LLM·임베딩 없이, 결정적).

같은 사건은 수십 곳이 거의 같은 제목으로 보도한다. 문서 목록이 사본으로 넘치지 않게, 문서를 색인한 직후(같은 유휴 묶음)
제목 낱말이 겹치는 최근 이슈에 붙이거나 새 이슈를 만든다.

- 비교 대상은 **제목 낱말만** 쓴다. 수집 문서 대부분이 본문을 저장하지 않는 언론 기사라, 본문 낱말을 섞으면 본문이 있는
  문서(공공기관·기타)와 없는 문서의 비교 기준이 달라진다. 제목은 편집자가 사건을 요약한 문장이라 사건 단위 비교에 맞다.
- 제목 앞뒤의 꼬리표([단독], [포토], (종합), [AI는 지금] 같은 연재명)는 떼고, 흔한 낱말(STOP_TERMS)은 뺀다.
  연재명을 떼지 않으면 한 언론사의 같은 연재가 서로 다른 사건인데도 묶인다.
- 이슈의 서명(top_terms)은 소속 문서 제목 낱말 중 많이 나온 순 최대 15개. 새 문서의 낱말 집합 D와 서명 S의
  겹침 계수 |D∩S| / min(|D|, |S|)가 기준(issue_threshold) 이상이고 겹친 낱말이 issue_min_shared(기본 2)개 이상이면
  가장 점수가 높은 이슈에 붙인다 (같으면 겹친 낱말 수, 그다음 최근 이슈). 서명이 커질수록 Jaccard는 이슈 크기 때문에
  낮아지므로 짧은 제목과 큰 서명을 비교하는 이 상황에는 겹침 계수가 맞다.
- 문서 시각 t = 발행일(없거나 수집 시각보다 1시간 넘게 미래면 수집 시각). 이슈의 first/last_seen_at은 소속 문서 t의 최소·최대다.
  수집 시각을 쓰지 않는 이유: 새 언론사를 등록하면 며칠 전 기사를 지금 수집하므로 끝난 이슈가 계속 살아 있게 된다.
- 후보 이슈: last_seen_at이 t - window 이후이고 first_seen_at이 t + window 이전. 지금 기준으로 window보다 오래된 이슈는
  CLOSED로 닫아 새 문서와 비교하지 않는다. 다만 예전 문서를 처음 색인할 때(t가 window보다 오래됨, 백필)는 그 시점 근처의
  닫힌 이슈와도 비교한다.
- 대표 제목: 서명과 가장 많이 겹치는 문서(가장 "가운데" 제목), 같으면 먼저 발행된 문서. 첫 보도는 [단독]처럼 좁은
  각도인 경우가 많아, 여러 언론사가 공통으로 쓴 표현을 담은 제목이 이슈를 더 잘 나타낸다.
- 같은 언론사의 중복 기사도 문서 수에 들어가고, source_count(서로 다른 소스 수)가 보도 범위를 보여 준다.
- 낱말이 2개 미만인 제목은 이슈를 만들지 않는다 (어디에도 붙을 수 없고 아무것도 끌어당기지 못한다).
"""

from __future__ import annotations

import html
import json
import logging
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import textindex

log = logging.getLogger(__name__)

TOP_TERMS = 15

# 사건을 가리키지 않는 흔한 제목 낱말. 기사 형식(단독·속보·포토), 날짜어(오늘·내일), 언론 일반어(기자·뉴스),
# 어디에나 붙는 주체(정부·대통령·한국)와 수식어. 정부·대통령·한국은 고유한 사건이어도 다른 낱말(인명·정책명)이
# 함께 겹쳐야 묶이도록 뺀다 — 실제 데이터에서 '李대통령 …' 제목끼리 다른 사건이 이 낱말 하나로 겹쳤다.
STOP_TERMS: frozenset[str] = frozenset({
    # 기사 형식
    "단독", "종합", "속보", "포토", "영상", "사진", "인터뷰", "르포", "기획", "칼럼", "사설", "오피니언", "포토뉴스",
    "뉴스", "기자", "특파원", "앵커", "브리핑", "리포트", "현장", "카드뉴스", "그래픽", "일문일답", "전문", "인사", "부고",
    # 날짜·시점
    "오늘", "내일", "어제", "올해", "내년", "작년", "지난해", "이번", "지난", "최근", "당시", "현재", "오전", "오후",
    # 주체·일반어
    "한국", "정부", "대통령", "국내", "세계", "관련", "이유", "논란", "가능성", "전망", "우려", "필요", "강조",
    "발표", "개최", "진행", "추진", "예정", "계획", "공개", "확인", "결과", "상황", "문제", "사람", "가지", "모습",
    "이상", "이하", "최대", "최고", "최초", "역대", "대해", "위해", "통해", "대한", "때문",
    # 'AI'는 2026-10 실제 제목 2,700건 중 가장 흔한 낱말(약 4%)이고, 기준 근처의 잘못된 결합(AI 데이터센터 ↔ AI 협력 등)에
    # 가장 자주 끼었다. 검색 색인에는 그대로 둔다
    "ai",
    # 영문 제목(영자 신문)의 기능어
    "the", "an", "to", "in", "on", "of", "for", "and", "or", "at", "by", "with", "as", "is", "are", "be", "from",
    "after", "over", "amid", "into", "its", "it", "his", "her", "their", "new", "says", "say", "will", "than", "more",
    "korea", "korean", "seoul",
})

# 제목 앞뒤 꼬리표: [단독] [포토] [AI는 지금] 【속보】 <인터뷰> (종합2보) — 괄호 안 12자 이하만 뗀다
_TAG_RE = re.compile(r"\[[^\]]{0,30}\]|【[^】]{0,30}】|<[^>]{0,20}>|〈[^〉]{0,20}〉|\([^)]{0,12}\)")


# 제목 끝의 매체·섹션 꼬리: "-스포츠ㅣ한국일보", " | 연합뉴스" — 같은 매체 기사끼리 이 낱말로 겹치지 않게 뗀다
_OUTLET_TAIL_RE = re.compile(r"\s*-\s*[^\s\-]{1,10}[ㅣᅵ|│][^\s\-]{1,12}$|\s+[|│]\s*[^|│]{1,20}$")


def clean_title(title: str | None) -> str:
    t = unicodedata.normalize("NFKC", html.unescape(title or ""))
    t = _OUTLET_TAIL_RE.sub("", t)
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", t)).strip()


def title_terms(title: str | None) -> list[str]:
    """이슈 비교용 제목 낱말 (중복 제거, 순서 유지, 흔한 낱말 제외)."""
    return [t for t in dict.fromkeys(textindex.terms(clean_title(title))) if t not in STOP_TERMS]


def doc_time(published_at: datetime | None, collected_at: datetime) -> datetime:
    """이슈 시각으로 쓸 문서 시각: 발행일, 없거나 수집보다 1시간 넘게 미래(시간대 오류)면 수집 시각."""
    if published_at is None or published_at > collected_at + timedelta(hours=1):
        return collected_at
    return published_at


def overlap(doc: set[str], sig: set[str]) -> tuple[int, float]:
    """(겹친 낱말 수, 겹침 계수)."""
    if not doc or not sig:
        return 0, 0.0
    shared = len(doc & sig)
    return shared, shared / min(len(doc), len(sig))


@dataclass
class Candidate:
    id: int
    top_terms: list[str]
    last_seen_at: datetime


@dataclass
class Match:
    issue_id: int
    shared: int
    score: float


@dataclass(frozen=True)
class Rule:
    """붙이는 기준. 겹친 낱말이 min_shared개 이상이고 겹침 계수가 threshold 이상. 겹친 낱말이 딱 2개(=pair)면
    겹침 계수가 pair_threshold 이상이어야 한다 — 실제 데이터에서 2개만 겹친 결합은 '서울·축제', 'AI·반도체'처럼
    흔한 낱말 둘이 우연히 겹친 경우가 많았다 (짧은 제목에서 2개가 거의 전부일 때만 믿는다)."""
    threshold: float = 0.4
    min_shared: int = 2
    pair_threshold: float = 0.66   # 2/3 = 0.667을 받아들인다 (3개 중 2개)

    @classmethod
    def of(cls, settings) -> "Rule":
        return cls(settings.issue_threshold, settings.issue_min_shared, settings.issue_pair_threshold)

    def accepts(self, shared: int, score: float) -> bool:
        if shared < self.min_shared or score < self.threshold:
            return False
        return shared > 2 or score >= self.pair_threshold


def best_match(doc: list[str] | set[str], candidates: list[Candidate], rule: Rule) -> Match | None:
    d = set(doc)
    best: tuple | None = None
    for c in candidates:
        shared, score = overlap(d, set(c.top_terms))
        if not rule.accepts(shared, score):
            continue
        key = (score, shared, c.last_seen_at, -c.id)
        if best is None or key > best[0]:
            best = (key, Match(c.id, shared, score))
    return best[1] if best else None


def top_terms(term_lists: list[list[str]], k: int = TOP_TERMS) -> list[str]:
    """소속 문서 제목 낱말 중 많이 나온 순 k개. 같은 횟수는 먼저 나온 순 (결정적)."""
    counts: Counter[str] = Counter()
    first: dict[str, int] = {}
    for terms in term_lists:
        for t in dict.fromkeys(terms):
            counts[t] += 1
            first.setdefault(t, len(first))
    return sorted(counts, key=lambda t: (-counts[t], first[t]))[:k]


@dataclass
class Member:
    document_id: int
    title: str | None
    terms: list[str]
    t: datetime


def representative(members: list[Member], sig: list[str]) -> Member:
    """서명과 가장 많이 겹치는 문서, 같으면 먼저 발행된 문서, 그다음 작은 id."""
    s = set(sig)
    return min(members, key=lambda m: (-len(s & set(m.terms)), m.t, m.document_id))


# ---------------------------------------------------------------------- 메모리 안 묶기 (보정·단위 테스트용)

@dataclass
class MemIssue:
    id: int
    members: list[Member] = field(default_factory=list)
    sources: list[int] = field(default_factory=list)
    top: list[str] = field(default_factory=list)
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    status: str = "OPEN"

    @property
    def title(self) -> str | None:
        return representative(self.members, self.top).title

    @property
    def source_count(self) -> int:
        return len(set(self.sources))


class MemoryClusterer:
    """DB 경로(assign_issues)와 같은 규칙을 메모리에서 돈다. 실제 제목으로 기준값을 보정할 때와 단위 테스트에 쓴다."""

    def __init__(self, window_hours: float = 72, rule: Rule | None = None, k: int = TOP_TERMS):
        self.window = timedelta(hours=window_hours)
        self.rule = rule or Rule()
        self.k = k
        self.issues: dict[int, MemIssue] = {}
        self.decisions: list[tuple[int, Match | None, list[str]]] = []
        self.rejected: list[tuple[int, Match, list[str]]] = []

    def close(self, now: datetime) -> int:
        n = 0
        for i in self.issues.values():
            if i.status == "OPEN" and i.last_seen < now - self.window:
                i.status = "CLOSED"
                n += 1
        return n

    def candidates(self, t: datetime, now: datetime) -> list[Candidate]:
        backfill = t < now - self.window
        return [Candidate(i.id, i.top, i.last_seen) for i in self.issues.values()
                if i.last_seen >= t - self.window and i.first_seen <= t + self.window
                and (i.status == "OPEN" or backfill)]

    def add(self, document_id: int, source_id: int, title: str | None, t: datetime,
            now: datetime | None = None) -> int | None:
        now = now or t
        terms = title_terms(title)
        if len(terms) < 2:
            self.decisions.append((document_id, None, terms))
            return None
        cands = self.candidates(t, now)
        m = best_match(terms, cands, self.rule)
        self.decisions.append((document_id, m, terms))
        if m is None:   # 보정용: 기준에 못 미친 가장 가까운 후보 (겹친 낱말 2개 이상)
            near = best_match(terms, cands, Rule(0.0, 2, 0.0))
            if near is not None:
                self.rejected.append((document_id, near, terms))
        if m is None:
            issue = MemIssue(id=len(self.issues) + 1)
            self.issues[issue.id] = issue
        else:
            issue = self.issues[m.issue_id]
        issue.members.append(Member(document_id, title, terms, t))
        issue.sources.append(source_id)
        issue.top = top_terms([x.terms for x in issue.members], self.k)
        issue.first_seen = min(x.t for x in issue.members)
        issue.last_seen = max(x.t for x in issue.members)
        if issue.last_seen >= now - self.window:
            issue.status = "OPEN"
        return issue.id


# ---------------------------------------------------------------------- DB 경로

ADVISORY_LOCK = 0x5E15_1550   # 이슈 묶기는 워커 사이에서 직렬화한다 (같은 사건에 이슈가 둘 생기지 않게)


@dataclass
class IssueOutcome:
    created: int = 0
    joined: int = 0
    skipped: int = 0     # 낱말이 모자라 이슈를 만들지 않은 문서
    closed: int = 0


def _detach(store, document_id: int) -> None:
    """다시 색인하는 문서를 원래 이슈에서 뗀다. 이슈가 비면 지우고, 아니면 통계를 다시 계산한다."""
    row = store.conn.execute("DELETE FROM issue_document WHERE document_id = %s RETURNING issue_id",
                             (document_id,)).fetchone()
    if row is None:
        return
    left = store.conn.execute("SELECT count(*) AS c FROM issue_document WHERE issue_id = %s",
                              (row["issue_id"],)).fetchone()["c"]
    if left == 0:
        store.conn.execute("DELETE FROM issue WHERE id = %s", (row["issue_id"],))
    else:
        refresh_issue(store, row["issue_id"], None)


def refresh_issue(store, issue_id: int, window_hours: float | None, k: int = TOP_TERMS) -> None:
    """소속 문서로 이슈의 대표 제목·시각·건수·언론사 수·서명을 다시 계산한다."""
    rows = store.conn.execute(
        """
        SELECT i.document_id, i.terms, d.title, d.source_id, d.published_at, d.collected_at
        FROM issue_document i JOIN document d ON d.id = i.document_id
        WHERE i.issue_id = %s ORDER BY i.document_id
        """, (issue_id,)).fetchall()
    members = [Member(r["document_id"], r["title"], list(r["terms"] or []), doc_time(r["published_at"], r["collected_at"]))
               for r in rows]
    sig = top_terms([m.terms for m in members], k)
    rep = representative(members, sig)
    reopen = window_hours is not None
    store.conn.execute(
        """
        UPDATE issue SET title = %s, first_seen_at = %s, last_seen_at = %s, doc_count = %s, source_count = %s,
               top_terms = %s::jsonb, representative_document_id = %s, updated_at = now(),
               status = CASE WHEN %s AND %s >= now() - (%s * interval '1 hour') THEN 'OPEN' ELSE status END
        WHERE id = %s
        """, ((rep.title or "")[:1000], min(m.t for m in members), max(m.t for m in members), len(members),
              len({r["source_id"] for r in rows}), json.dumps(sig, ensure_ascii=False), rep.document_id,
              reopen, max(m.t for m in members), window_hours or 0, issue_id))


def assign_issue(store, doc: dict, settings, outcome: IssueOutcome) -> None:
    """색인 직후 문서 하나를 이슈에 붙이거나 새 이슈를 만든다. doc: id, title, published_at, collected_at.
    호출하는 쪽이 트랜잭션과 advisory lock을 잡는다."""
    _detach(store, doc["id"])
    terms = title_terms(doc["title"])
    if len(terms) < 2:
        outcome.skipped += 1
        return
    t = doc_time(doc["published_at"], doc["collected_at"])
    window = settings.issue_window_hours
    rows = store.conn.execute(
        """
        SELECT id, top_terms, last_seen_at FROM issue
        WHERE top_terms ?| %s::text[]
          AND last_seen_at >= %s - (%s * interval '1 hour')
          AND first_seen_at <= %s + (%s * interval '1 hour')
          AND (status = 'OPEN' OR %s < now() - (%s * interval '1 hour'))
        """, (terms, t, window, t, window, t, window)).fetchall()
    cands = [Candidate(r["id"], list(r["top_terms"] or []), r["last_seen_at"]) for r in rows]
    m = best_match(terms, cands, Rule.of(settings))
    if m is None:
        issue_id = store.conn.execute(
            """
            INSERT INTO issue (title, first_seen_at, last_seen_at, doc_count, source_count, top_terms)
            VALUES (%s, %s, %s, 0, 0, '[]'::jsonb) RETURNING id
            """, ((doc["title"] or "")[:1000], t, t)).fetchone()["id"]
        score = 1.0
        outcome.created += 1
    else:
        issue_id, score = m.issue_id, m.score
        outcome.joined += 1
    store.conn.execute(
        "INSERT INTO issue_document (issue_id, document_id, similarity, terms) VALUES (%s, %s, %s, %s::jsonb)",
        (issue_id, doc["id"], round(score, 4), json.dumps(terms, ensure_ascii=False)))
    refresh_issue(store, issue_id, window)


def close_stale(store, window_hours: float) -> int:
    """마지막 문서가 window보다 오래된 이슈를 닫는다 (새 문서와 더는 비교하지 않는다)."""
    return store.conn.execute(
        """
        UPDATE issue SET status = 'CLOSED', updated_at = now()
        WHERE status = 'OPEN' AND last_seen_at < now() - (%s * interval '1 hour')
        """, (window_hours,)).rowcount
