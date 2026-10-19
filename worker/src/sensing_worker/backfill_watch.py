"""이미 수집한 문서에 키워드 감시를 한 번 대조한다 (감시 대조 기능이 들어오기 전에 쌓인 문서용, 또는 새 감시를 만든 뒤).

    python -m sensing_worker.backfill_watch [--days 30]

수집 때 하는 대조(jobs.JobRunner._match_watches)와 같은 규칙이고, 이미 기록된 감시·문서 쌍은 건너뛴다.
"""

from __future__ import annotations

import argparse
import logging

from .config import Settings
from .db import Store
from .watchmatch import from_configs, match_keywords

log = logging.getLogger("sensing_worker.backfill_watch")


def backfill(store: Store, days: int) -> tuple[int, int]:
    """(대조한 문서 수, 새로 남긴 감시 기록 수)."""
    watches = from_configs(store.load_watch_keywords())
    if not watches:
        return 0, 0
    rows = store.conn.execute(
        "SELECT id, title, body_text FROM document WHERE collected_at >= now() - make_interval(days => %s) ORDER BY id",
        (days,)).fetchall()
    added = 0
    for r in rows:
        for watch_id, keyword in match_keywords(r["title"], r["body_text"], watches):
            cur = store.conn.execute(
                """
                INSERT INTO document_watch (document_id, source_id, keyword) VALUES (%s, %s, %s)
                ON CONFLICT (document_id, source_id) DO NOTHING RETURNING document_id
                """, (r["id"], watch_id, keyword[:200])).fetchone()
            if cur is not None:
                added += 1
                company_id = store.company_of_watch(watch_id)
                if company_id is not None:
                    store.add_document_company(r["id"], company_id, "WATCH")
    return len(rows), added


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="이미 수집한 문서에 키워드 감시를 대조한다")
    parser.add_argument("--days", type=int, default=30, help="최근 며칠 동안 수집한 문서를 볼지 (기본 30)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    store = Store(Settings().db_url)
    try:
        checked, added = backfill(store, args.days)
    finally:
        store.close()
    log.info("watch backfill: checked=%s documents, added=%s hits", checked, added)


if __name__ == "__main__":
    main()
