"""이미 보강한 문서의 자사·경쟁사·고객사·협력사 라벨을 회사 관계 목록으로 다시 붙인다 (LLM 호출 없음).

    python -m sensing_worker.relabel [--dry-run] [--days N]

자사·경쟁사를 LLM이 고르던 때(2026-10-20 이전) 붙은 라벨을 지우고, 지금 목록(company_relation)으로 보강 때와 같은 규칙
(relations.relation_labels + merge_topics)을 적용한다. 관리자가 목록을 바꾼 뒤 이미 보강한 문서에 반영할 때도 쓴다.
- 대상: 보강이 DONE인 문서 (--days면 최근 N일 동안 수집한 것만). SKIPPED·FAILED는 분류가 없어 그대로 둔다.
- 보는 글: 제목, 저장된 본문(공공 문서), 수집 때 본문까지 보고 남긴 기록(document_relation). 언론 기사는 본문을 저장하지 않으므로
  이 기능 전(기록 없음)에 수집한 기사는 제목에서만 찾는다.
- LLM 분류(정책·규제 등)는 그대로 두고, 관계 라벨만 지우고 다시 붙인다. 관계 라벨이 붙으면 "기타"는 빼고, 남는 분류가 없으면 "기타".
- 실행 중인 워커가 그 사이 같은 문서를 다시 보강했으면(분류가 읽은 뒤 바뀜) 건너뛴다.
- --dry-run은 읽기 전용 트랜잭션에서 셈만 한다 (바뀌는 예 일부를 로그에 보인다). 실제 실행도 트랜잭션 하나다.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import Counter
from dataclasses import dataclass, field

from .config import Settings
from .db import Store
from .relations import RELATION_TOPIC_NAMES, from_rows, merge_topics, relation_labels

log = logging.getLogger("sensing_worker.relabel")

SAMPLES = 30   # 로그에 보일 바뀌는 문서 수


@dataclass
class RelabelResult:
    checked: int = 0                                     # 본 보강 수 (DONE)
    changed: int = 0                                     # 분류가 바뀐(바뀔) 문서 수
    skipped: int = 0                                     # 읽은 뒤 워커가 다시 보강해 건너뛴 수 (실제 실행만)
    before: Counter = field(default_factory=Counter)     # 관계 라벨별 지금 건수
    after: Counter = field(default_factory=Counter)      # 관계 라벨별 다시 붙인 뒤 건수
    removed: Counter = field(default_factory=Counter)    # 라벨별 지운 수
    added: Counter = field(default_factory=Counter)      # 라벨별 새로 붙인 수
    became_other: int = 0                                # 관계 라벨만 있다가 "기타"가 된 문서 수
    samples: list[tuple[int, list[str], list[str], str]] = field(default_factory=list)   # (문서, 전, 후, 제목)


def relabel(store: Store, dry_run: bool = False, days: int | None = None) -> RelabelResult:
    """보강이 DONE인 문서의 관계 라벨을 지금 목록으로 다시 붙인다 (모듈 설명 참고). dry_run이면 쓰지 않는다."""
    result = RelabelResult()
    relations = from_rows(store.load_company_relations())
    with store.conn.transaction():
        if dry_run:
            store.conn.execute("SET TRANSACTION READ ONLY")
        where, params = "e.status = 'DONE'", []
        if days is not None:
            where += " AND d.collected_at >= now() - make_interval(days => %s)"
            params.append(days)
        rows = store.conn.execute(
            f"SELECT e.document_id, e.topics, d.title, d.body_text FROM document_enrichment e "
            f"JOIN document d ON d.id = e.document_id WHERE {where} ORDER BY e.document_id", params).fetchall()
        recorded: dict[int, list[tuple[int, str]]] = {}
        if relations and rows:
            for r in store.conn.execute(
                    f"SELECT r.document_id, r.relation_id, r.matched FROM document_relation r "
                    f"JOIN document_enrichment e ON e.document_id = r.document_id JOIN document d ON d.id = r.document_id "
                    f"WHERE {where}", params):
                recorded.setdefault(r["document_id"], []).append((r["relation_id"], r["matched"]))

        updates: list[tuple[int, list[str], list[str]]] = []
        for r in rows:
            result.checked += 1
            old = [t for t in (r["topics"] or []) if isinstance(t, str)]
            labels = relation_labels(r["title"], r["body_text"], recorded.get(r["document_id"], []), relations)
            new = merge_topics(old, labels)
            for t in RELATION_TOPIC_NAMES:
                result.before[t] += t in old
                result.after[t] += t in new
                result.removed[t] += t in old and t not in new
                result.added[t] += t in new and t not in old
            if new == old:
                continue
            updates.append((r["document_id"], old, new))
            result.became_other += new == ["기타"]
            if len(result.samples) < SAMPLES:
                result.samples.append((r["document_id"], old, new, (r["title"] or "")[:100]))
        result.changed = len(updates)
        if dry_run:
            return result
        for document_id, old, new in updates:
            done = store.conn.execute(
                "UPDATE document_enrichment SET topics = %s::jsonb WHERE document_id = %s AND topics = %s::jsonb "
                "RETURNING document_id", (_json(new), document_id, _json(old))).fetchone()
            result.skipped += done is None
        result.changed -= result.skipped
    return result


def _json(topics: list[str]) -> str:
    return json.dumps(topics, ensure_ascii=False)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="보강한 문서의 자사·경쟁사 등 관계 라벨을 회사 관계 목록으로 다시 붙인다 (LLM 호출 없음)")
    parser.add_argument("--dry-run", action="store_true", help="셈만 하고 쓰지 않는다 (바뀌는 예 일부를 로그에 보인다)")
    parser.add_argument("--days", type=int, default=None, help="최근 며칠 동안 수집한 문서만 (기본: 전부)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    store = Store(Settings().db_url)
    try:
        r = relabel(store, dry_run=args.dry_run, days=args.days)
        for document_id, old, new, title in r.samples:
            log.info("  document=%s %s -> %s title=%r", document_id, old, new, title)
        per_label = ", ".join(f"{t} {r.before[t]}->{r.after[t]} (-{r.removed[t]} +{r.added[t]})" for t in RELATION_TOPIC_NAMES)
        log.info("relabel%s: checked=%s enrichments, changed=%s, became_other=%s, skipped(re-enriched meanwhile)=%s; %s",
                 " (dry run, nothing written)" if args.dry_run else "", r.checked, r.changed, r.became_other, r.skipped,
                 per_label)
    finally:
        store.close()


if __name__ == "__main__":
    main()
