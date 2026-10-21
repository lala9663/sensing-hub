"""회사 관계 라벨: 문서 분류의 자사·경쟁사·고객사·협력사를 LLM이 아니라 관리자가 정한 목록(company_relation)으로 붙인다.

LLM에게 자사·경쟁사를 고르게 했더니 "자사"를 '기사 속 그 회사 자신', "경쟁사"를 '아무 회사'로 읽었다 (2026-10-20 운영 DB 실측:
"광동제약, 창립 63주년" → 자사, "이케아, 어린이 컬렉션 출시" → 경쟁사). 누가 자사·경쟁사인지는 고객(삼성 DS)이 정할 일이라
관리자 메뉴 "자사·경쟁사" 목록(이름·별칭·제외어·관계)으로 정하고, 여기서 결정적으로 붙인다.

- 대조 규칙은 키워드 감시와 같다 (watchmatch: 대소문자·공백 무시, 한글 부분 일치, 영문 단어 경계).
  "Samsung Electronics"는 "Samsung Electronicsx"에 안 걸리고, "삼성전자"는 "삼성전자가"·"삼성전자서비스"에 걸린다.
- 제외어는 그 자리만 지우고 본다 (감시의 제외어처럼 문서 전체를 건너뛰지 않는다). '인텔'의 제외어 '인텔리전스'면
  "인텔리전스 시대"는 안 걸리고 "인텔리전스 시대, 인텔의 반격"은 걸린다.
- 어디를 보나: 문서 제목과 저장된 본문(공공 문서). 언론 기사는 본문을 저장하지 않으므로 수집 때 감시 대조와 같은 시점에
  본문까지 본 결과를 document_relation에 남겨 두고(jobs.JobRunner._match_watches) 보강·다시 라벨링 때 합친다.
  남은 기록은 그 관계 항목이 아직 있고 걸린 이름이 지금 이름·별칭에 있을 때만 쓴다 (별칭을 빼면 그 별칭 기록은 버린다).
- 최종 분류 = LLM 분류(관계 라벨 제외 7개) + 관계 라벨. 관계 라벨이 붙으면 "기타"는 뺀다 ("기타"는 어디에도 맞지 않을 때만).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .watchmatch import contains, mask, normalize

# 관계 코드 → 분류 이름 (분류에 붙는 순서)
RELATION_TOPICS: dict[str, str] = {"OWN": "자사", "COMPETITOR": "경쟁사", "CUSTOMER": "고객사", "PARTNER": "협력사"}
RELATION_TOPIC_NAMES: tuple[str, ...] = tuple(RELATION_TOPICS.values())
OTHER = "기타"


@dataclass(frozen=True)
class Relation:
    id: int
    relation: str                  # OWN | COMPETITOR | CUSTOMER | PARTNER
    names: tuple[str, ...]         # 이름 + 별칭 (빈 것·정규화해 같은 것은 뺀다)
    exclude: tuple[str, ...] = ()

    @property
    def topic(self) -> str:
        return RELATION_TOPICS[self.relation]

    def has_name(self, matched: str | None) -> bool:
        """수집 때 걸린 이름(document_relation.matched)이 지금도 이 항목의 이름·별칭인지."""
        k = normalize(matched)
        return bool(k) and any(normalize(n) == k for n in self.names)


def _strings(raw) -> list[str]:
    return [str(x).strip() for x in (raw or []) if str(x).strip()]


def from_rows(rows: list[dict]) -> list[Relation]:
    """db.load_company_relations 결과({"id", "name", "aliases", "exclude", "relation"}) → Relation (모르는 관계는 뺀다)."""
    out: list[Relation] = []
    for r in rows:
        if r.get("relation") not in RELATION_TOPICS:
            continue
        names: list[str] = []
        for n in [r.get("name")] + _strings(r.get("aliases")):
            n = str(n or "").strip()
            if n and normalize(n) not in {normalize(x) for x in names}:
                names.append(n)
        if names:
            out.append(Relation(int(r["id"]), r["relation"], tuple(names), tuple(_strings(r.get("exclude")))))
    return out


def match_relations(title: str | None, body: str | None, relations: list[Relation]) -> list[tuple[int, str]]:
    """(관계 항목 id, 걸린 이름·별칭) 목록. 항목마다 최대 하나(이름 → 별칭 순서대로 처음 걸린 것)."""
    haystack = normalize(title) + "\n" + normalize(body)
    if not haystack.strip():
        return []
    hits: list[tuple[int, str]] = []
    for r in relations:
        text = haystack
        for word in r.exclude:
            text = mask(text, word)
        for name in r.names:
            if contains(text, name):
                hits.append((r.id, name))
                break
    return hits


def relation_labels(title: str | None, body: str | None, recorded: Iterable[tuple[int, str]],
                    relations: list[Relation]) -> list[str]:
    """이 문서에 붙일 관계 라벨 (RELATION_TOPIC_NAMES 순서). recorded: 수집 때 남긴 (관계 항목 id, 걸린 이름) — document_relation."""
    by_id = {r.id: r for r in relations}
    hit_ids = {relation_id for relation_id, _ in match_relations(title, body, relations)}
    hit_ids |= {relation_id for relation_id, matched in recorded
                if relation_id in by_id and by_id[relation_id].has_name(matched)}
    labels = {by_id[i].topic for i in hit_ids}
    return [t for t in RELATION_TOPIC_NAMES if t in labels]


def merge_topics(llm_topics: Iterable[str], labels: Iterable[str]) -> list[str]:
    """최종 분류: LLM 분류에서 관계 라벨을 빼고(LLM이 보내도 버린다) 결정적 관계 라벨을 더한다.

    관계 라벨이 있으면 "기타"를 빼고, 남는 것이 없으면 ["기타"].
    """
    labels = [t for t in dict.fromkeys(labels) if t in RELATION_TOPIC_NAMES]
    base = [t for t in dict.fromkeys(llm_topics) if t not in RELATION_TOPIC_NAMES]
    if labels:
        base = [t for t in base if t != OTHER]
    return base + labels or [OTHER]
