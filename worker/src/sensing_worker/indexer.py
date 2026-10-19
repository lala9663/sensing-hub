"""수집 뒤 단계: 전문 검색 색인(chunk.tokens)과 이슈 묶기 (M5 기반, LLM 없이 결정적).

큐에 작업이 없을 때 main 루프가 index_pending을 한 묶음(기본 200건)씩 돌린다. 한 트랜잭션 안에서
① 색인할 문서를 잠그고(FOR UPDATE SKIP LOCKED) ② 기존 조각을 지우고 제목(seq 0)·본문 조각을 넣고 ③ indexed_at을 채우고
④ 문서마다 이슈에 붙이거나 새 이슈를 만든다(워커 사이 advisory lock으로 직렬화). 끝나면 오래된 이슈를 닫는다.

- 언론 기사는 본문을 저장하지 않으므로 제목만 색인한다. NEWS 사이트는 본문이 있어도 제목만 쓴다 (보강의 is_press와 같은 규칙).
- 이미 쌓인 문서는 유휴 틱마다 조금씩 색인되고, 한 번에 끝내려면 백필 명령을 쓴다.

    sensing-index-backfill [--limit N]
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field

from . import issues, textindex
from .config import Settings

log = logging.getLogger(__name__)


@dataclass
class IndexOutcome:
    fetched: int = 0
    chunks: int = 0
    issues: issues.IssueOutcome = field(default_factory=issues.IssueOutcome)


def _body_for_index(row: dict) -> str | None:
    if row.get("site_type") == "NEWS":
        return None
    return row.get("body_text")


def index_pending(store, settings: Settings, limit: int | None = None) -> IndexOutcome:
    """색인이 필요한 문서를 limit건 색인하고 이슈에 붙인다. 오류는 그대로 올라간다 (트랜잭션은 되돌려진다)."""
    limit = limit or settings.index_batch
    out = IndexOutcome()
    version = textindex.index_version()
    with store.conn.transaction():
        rows = store.lock_pending_index(limit)
        out.fetched = len(rows)
        if rows:
            ids = [r["id"] for r in rows]
            store.conn.execute("DELETE FROM chunk WHERE document_id = ANY(%s) AND attachment_id IS NULL", (ids,))
            params = []
            for r in rows:
                for seq, text in enumerate(textindex.chunk_document(r["title"], _body_for_index(r))):
                    params.append((r["id"], seq, text, textindex.tsv_text(textindex.index_terms(text)), version))
            if params:
                with store.conn.cursor() as cur:
                    cur.executemany(
                        """
                        INSERT INTO chunk (document_id, seq, text, tokens, index_version)
                        VALUES (%s, %s, %s, to_tsvector('simple', %s), %s)
                        """, params)
            out.chunks = len(params)
            store.conn.execute("UPDATE document SET indexed_at = now() WHERE id = ANY(%s)", (ids,))

            # 이슈 묶기: 워커끼리 직렬화하고, 묶음 안에서는 시간순으로 붙인다 (도착 순서를 흉내 낸다)
            store.conn.execute("SELECT pg_advisory_xact_lock(%s)", (issues.ADVISORY_LOCK,))
            for r in sorted(rows, key=lambda r: (issues.doc_time(r["published_at"], r["collected_at"]), r["id"])):
                issues.assign_issue(store, r, settings, out.issues)
    out.issues.closed = issues.close_stale(store, settings.issue_window_hours)
    if out.fetched or out.issues.closed:
        log.info("index: documents=%s chunks=%s analyzer=%s issues created=%s joined=%s skipped=%s closed=%s",
                 out.fetched, out.chunks, version, out.issues.created, out.issues.joined, out.issues.skipped,
                 out.issues.closed)
    return out


def backfill(store, settings: Settings, limit: int | None = None) -> int:
    """색인할 문서가 없을 때까지(또는 limit건까지) index_pending을 되풀이한다. 색인한 문서 수."""
    total = 0
    while limit is None or total < limit:
        batch = settings.index_batch if limit is None else min(settings.index_batch, limit - total)
        n = index_pending(store, settings, batch).fetched
        total += n
        if n == 0:
            break
    return total


def main(argv: list[str] | None = None) -> None:
    from .db import Store

    parser = argparse.ArgumentParser(description="이미 수집한 문서를 전문 검색 색인하고 이슈로 묶는다 (한 번 실행)")
    parser.add_argument("--limit", type=int, default=None, help="최대 문서 수 (기본: 남은 문서 전부)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings()
    store = Store(settings.db_url)
    try:
        done = backfill(store, settings, args.limit)
    finally:
        store.close()
    log.info("index backfill: indexed=%s documents (analyzer=%s)", done, textindex.analyzer())


if __name__ == "__main__":
    main()
