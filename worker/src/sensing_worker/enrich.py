"""수집 시점 요약·분류(보강): 수집된 문서마다 LLM으로 한 줄 요약과 분류 태그를 붙여 document_enrichment에 둔다.

수집과 분리돼 있다. 큐에 작업이 없을 때 main 루프가 한 묶음씩 돌리며, LLM 장애(LlmUnavailable)면 아무것도 쓰지 않고
돌아가 다음 루프에 다시 시도한다. 검증 실패는 FAILED로 기록해 무한 반복을 막되 24시간 뒤 다시 대상이 된다.

- 분류 체계는 고정(TOPICS)이고 체계 밖 값은 버린다. 자유 태그는 최대 5개.
- 언론 기사(본문 미저장 또는 NEWS 사이트)는 제목만으로 분류하고 summary는 None으로 강제한다.
  제목만 저장하는 저작권 원칙을 지키기 위해 요약을 만들어 저장하지 않는다.
- 한 번의 호출에 문서 여러 건(기본 10)을 번호를 붙여 묶고, 출력은 {items:[{idx, summary, topics, tags}]}다.
  JSON 꺼내기·스키마 검증·1회 재요청은 llm.generate_recipe와 같은 공통 경로를 쓴다.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from .config import Settings
from .llm import Generation, LlmClient, LlmUnavailable, describe, generate_recipe

log = logging.getLogger(__name__)

TOPICS: tuple[str, ...] = ("정책·규제", "입찰·공고", "지원사업", "자사", "경쟁사", "산업동향", "인사·조직", "사건·사고", "기타")
MAX_TAGS = 5
MAX_TAG_CHARS = 100
MAX_SUMMARY_CHARS = 600
FAILED_RETRY_HOURS = 24

ENRICH_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["idx", "summary", "topics", "tags"],
                "properties": {
                    "idx": {"type": "integer", "description": "입력의 [문서 N] 번호"},
                    "summary": {"type": ["string", "null"], "description": "본문이 있는 문서의 2문장 이내 요약. 본문이 없으면 null"},
                    "topics": {"type": "array", "items": {"type": "string"},
                               "description": "분류 체계 안의 값만 1~3개: " + ", ".join(TOPICS)},
                    "tags": {"type": "array", "items": {"type": "string"},
                             "description": "자유 태그 최대 5개 (인명·기관명·제품명 등)"},
                },
            },
        },
    },
}

SYSTEM_PROMPT = f"""당신은 대외협력(정책·규제·공고)과 커뮤니케이션(자사·경쟁사 보도) 담당자를 위해 수집된 문서를 분류하고 요약하는 분석가입니다.
번호가 붙은 문서 여러 건을 받아 문서마다 요약·분류·태그를 JSON 객체 하나로 돌려줍니다.

규칙
- JSON 객체만 출력합니다. 설명 문장, 코드, 마크다운을 덧붙이지 않습니다.
- 출력은 {{"items": [{{"idx": 번호, "summary": ..., "topics": [...], "tags": [...]}}, ...]}} 형식이며,
  입력의 모든 [문서 N]에 대해 idx가 N인 항목을 정확히 하나씩 냅니다. 입력에 없는 번호는 만들지 않습니다.
- summary: 본문이 있는 문서만 한국어 2문장 이내(300자 이내)로 핵심을 씁니다. "본문: (없음 — 언론 기사, 제목만 저장)"인 문서는
  요약하지 않고 반드시 null로 둡니다. 제목을 되풀이하지 말고 날짜·금액·대상처럼 담당자가 바로 쓸 사실을 담습니다.
- topics: 아래 분류 체계의 값만 1~3개 고릅니다. 다른 표현을 만들지 않습니다. 어디에도 맞지 않으면 "기타"만 씁니다.
  · 정책·규제: 법령·고시·규제 변경, 정부·국회·규제기관의 방침
  · 입찰·공고: 입찰·공모·사업자 선정·조달 공고
  · 지원사업: 보조금·지원 프로그램·공모 사업·바우처
  · 자사: 우리 회사(출처 줄의 감시 이름이 자사·회사명인 키워드) 관련 보도·언급
  · 경쟁사: 경쟁사·동종 업계 회사 관련 보도
  · 산업동향: 시장·기술·업계 흐름, 통계, 전망
  · 인사·조직: 임원 인사·조직 개편·채용
  · 사건·사고: 장애·사고·소송·제재·논란
  · 기타: 위 어디에도 맞지 않음
  출처 줄에 감시 이름과 검색어가 있으면 참고합니다 (예: 감시 '자사 보도'에 걸린 기사는 보통 자사, 감시 '경쟁사'는 경쟁사).
- tags: 문서에 실제로 나온 인명·기관명·회사명·제품명·법령명 같은 고유명사를 최대 {MAX_TAGS}개. 일반 명사는 넣지 않습니다. 없으면 빈 배열.

출력 예시 (형식 참고용이며 값은 입력과 무관합니다)
{{"items": [
  {{"idx": 1, "summary": "국토교통부가 10월 20일부터 드론 비행 승인 절차를 간소화한다. 150m 이하 비행은 사전 승인이 면제된다.", "topics": ["정책·규제"], "tags": ["국토교통부", "항공안전법"]}},
  {{"idx": 2, "summary": null, "topics": ["자사", "산업동향"], "tags": ["메타넷", "AI 컨택센터"]}}
]}}
"""

_DOC_MARK = re.compile(r"^\[문서 (\d+)\]", re.M)


@dataclass
class PendingDoc:
    """lock_pending_enrichment 한 행 + 감시 기록."""
    id: int
    version: int
    url: str
    title: str | None
    body: str | None
    domain: str
    section: str | None = None
    site_type: str = "UNKNOWN"
    watches: list[tuple[str | None, str | None]] = field(default_factory=list)   # (감시 이름, 검색어)

    @property
    def is_press(self) -> bool:
        """언론 기사: 본문을 저장하지 않았거나 NEWS 사이트. 제목만 분류하고 요약은 만들지 않는다."""
        return self.site_type == "NEWS" or not (self.body or "").strip()

    @property
    def is_empty(self) -> bool:
        return not (self.title or "").strip() and not (self.body or "").strip()


@dataclass
class Enrichment:
    summary: str | None
    topics: list[str]
    tags: list[str]
    status: str = "DONE"
    error: str | None = None


@dataclass
class EnrichOutcome:
    fetched: int = 0
    done: int = 0
    failed: int = 0
    skipped: int = 0
    llm_unavailable: bool = False
    usage: dict[str, int] = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})


# ---------------------------------------------------------------------- 입력 만들기

def build_batch_prompt(docs: list[PendingDoc], body_chars: int) -> str:
    parts = [f"문서 {len(docs)}건입니다. 문서마다 idx가 번호와 같은 항목을 하나씩 내세요."]
    for i, d in enumerate(docs, 1):
        source = [f"도메인 {d.domain}"]
        if d.section:
            source.append(f"섹션 {d.section}")
        if d.site_type == "NEWS":
            source.append("언론사")
        for watch_name, keyword in d.watches:
            hit = f"감시 '{watch_name}'" if watch_name else "감시"
            if keyword:
                hit += f" 검색어 '{keyword}'"
            source.append(hit)
        parts.append(f"\n[문서 {i}]")
        parts.append("출처: " + " / ".join(source))
        parts.append(f"제목: {(d.title or '').strip() or '(없음)'}")
        if d.is_press:
            parts.append("본문: (없음 — 언론 기사, 제목만 저장)")
        else:
            body = re.sub(r"\s+", " ", d.body or "").strip()
            if len(body) > body_chars:
                body = body[:body_chars] + " ...(잘림)"
            parts.append(f"본문: {body}")
    parts.append("\n위 문서들을 규칙에 따라 분류·요약한 JSON 객체를 출력하세요.")
    return "\n".join(parts)


# ---------------------------------------------------------------------- 응답 해석

def check_items(data: dict, count: int) -> list[str]:
    """스키마 밖의 빠른 검사: 번호가 1..count를 정확히 한 번씩 덮어야 한다. 오류는 재요청 프롬프트에 들어간다."""
    errors: list[str] = []
    seen: set[int] = set()
    for item in data.get("items") or []:
        idx = item.get("idx")
        if not isinstance(idx, int) or idx < 1 or idx > count:
            errors.append(f"items: idx {idx!r}는 입력에 없는 번호입니다 (1~{count})")
        elif idx in seen:
            errors.append(f"items: idx {idx}가 두 번 나왔습니다")
        seen.add(idx if isinstance(idx, int) else -1)
    missing = [i for i in range(1, count + 1) if i not in seen]
    if missing:
        errors.append(f"items: 번호 {missing}의 항목이 없습니다. 모든 문서에 대해 하나씩 내세요")
    return errors


def _clean_tags(raw) -> list[str]:
    out: list[str] = []
    for t in raw or []:
        if not isinstance(t, str):
            continue
        t = re.sub(r"\s+", " ", t).strip()[:MAX_TAG_CHARS]
        if t and t not in out:
            out.append(t)
        if len(out) >= MAX_TAGS:
            break
    return out


def _clean_topics(raw) -> list[str]:
    out = [t.strip() for t in (raw or []) if isinstance(t, str)]
    out = [t for t in dict.fromkeys(out) if t in TOPICS]      # 체계 밖 값은 버리고 순서는 유지
    return out or ["기타"]


def parse_items(data: dict, docs: list[PendingDoc]) -> dict[int, Enrichment]:
    """{idx: Enrichment}. 입력에 없는 번호는 무시하고 같은 번호가 여럿이면 첫 것을 쓴다.

    언론 기사는 모델이 요약을 써 보내도 summary를 None으로 강제한다.
    """
    out: dict[int, Enrichment] = {}
    for item in data.get("items") or []:
        idx = item.get("idx")
        if not isinstance(idx, int) or idx < 1 or idx > len(docs) or idx in out:
            continue
        doc = docs[idx - 1]
        summary = item.get("summary")
        if doc.is_press or not isinstance(summary, str) or not summary.strip():
            summary = None
        else:
            summary = re.sub(r"\s+", " ", summary).strip()[:MAX_SUMMARY_CHARS]
        out[idx] = Enrichment(summary, _clean_topics(item.get("topics")), _clean_tags(item.get("tags")))
    return out


# ---------------------------------------------------------------------- 묶음 보강

def enrich_batch(llm: LlmClient, docs: list[PendingDoc], settings: Settings) -> tuple[dict[int, Enrichment], Generation]:
    """문서 묶음 하나를 LLM에 보내 {idx: Enrichment}를 만든다. LlmUnavailable은 그대로 올라간다.

    검증이 두 번 실패하면 묶음의 모든 문서를 FAILED Enrichment로 돌려준다 (24시간 뒤 다시 대상이 된다).
    """
    user = build_batch_prompt(docs, settings.enrich_body_chars)
    gen = generate_recipe(llm, SYSTEM_PROMPT, user, ENRICH_SCHEMA, check=lambda d: check_items(d, len(docs)))
    if gen.data is None:
        error = f"LLM 응답 검증 실패 ({gen.attempts}회): " + "; ".join(gen.errors)
        return {i: Enrichment(None, [], [], status="FAILED", error=error[:500]) for i in range(1, len(docs) + 1)}, gen
    return parse_items(gen.data, docs), gen


def _to_pending(row: dict, watches: dict[int, list[tuple[str | None, str | None]]]) -> PendingDoc:
    return PendingDoc(id=row["id"], version=row["version"], url=row["url"], title=row["title"], body=row["body_text"],
                      domain=row["domain"], section=row["section"], site_type=row["site_type"] or "UNKNOWN",
                      watches=watches.get(row["id"], []))


def enrich_pending(store, llm: LlmClient, settings: Settings, limit: int | None = None) -> EnrichOutcome:
    """보강이 필요한 문서를 최신순으로 limit건 잠가(FOR UPDATE SKIP LOCKED) 보강하고 저장한다.

    LlmUnavailable이면 아무것도 쓰지 않고 돌아간다 (잠금은 풀리고 다음 루프에 다시 시도). 제목·본문이 모두 없는 문서는
    LLM에 보내지 않고 SKIPPED로 적는다. 토큰 사용량은 로그에 남긴다.
    """
    limit = limit or settings.enrich_batch
    outcome = EnrichOutcome()
    with store.conn.transaction():
        rows = store.lock_pending_enrichment(limit, FAILED_RETRY_HOURS)
        if not rows:
            return outcome
        watches = store.load_watch_hits([r["id"] for r in rows])
        docs = [_to_pending(r, watches) for r in rows]
        outcome.fetched = len(docs)
        model = describe(llm)

        to_send = [d for d in docs if not d.is_empty]
        results: dict[int, Enrichment] = {}
        gen: Generation | None = None
        if to_send:
            try:
                results, gen = enrich_batch(llm, to_send, settings)
            except LlmUnavailable as e:
                log.warning("enrich skipped (LLM unavailable, nothing written): %s", e)
                outcome.llm_unavailable = True
                return outcome
            outcome.usage = dict(gen.usage)

        for d in docs:
            if d.is_empty:
                store.save_enrichment(d.id, d.version, summary=None, topics=[], tags=[], model=model,
                                      status="SKIPPED", error="제목과 본문이 없습니다")
                outcome.skipped += 1
                continue
            r = results.get(to_send.index(d) + 1)
            if r is None:   # 번호가 빠진 경우는 check_items가 걸러 재요청하므로 드물지만, 그래도 FAILED로 남긴다
                r = Enrichment(None, [], [], status="FAILED", error="LLM 응답에 이 문서의 항목이 없습니다")
            store.save_enrichment(d.id, d.version, summary=r.summary, topics=r.topics, tags=r.tags, model=model,
                                  status=r.status, error=r.error)
            if r.status == "DONE":
                outcome.done += 1
            else:
                outcome.failed += 1
    log.info("enrich: fetched=%s done=%s failed=%s skipped=%s llm=%s attempts=%s %s",
             outcome.fetched, outcome.done, outcome.failed, outcome.skipped, model,
             gen.attempts if gen else 0, gen.usage_text() if gen else "")
    return outcome
