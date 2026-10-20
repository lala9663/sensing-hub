"""이미 수집한 문서에 키워드 감시를 한 번 대조한다 (감시 대조 기능이 들어오기 전에 쌓인 문서용, 또는 새 감시를 만든 뒤).

    python -m sensing_worker.backfill_watch [--days 30]
    python -m sensing_worker.backfill_watch --recheck [--days 30] [--dry-run] [--drop-unverifiable]

수집 때 하는 대조(jobs.JobRunner._match_watches)와 같은 규칙이고, 이미 기록된 감시·문서 쌍은 건너뛴다.

--recheck는 대조 규칙이 바뀐 뒤(2026-10-20 영문 단어 경계: "DS"가 "applauds"에 걸리던 것) 이미 남은 기록을 지금 규칙으로 다시 판정한다.
기록마다 그 검색어가 저장된 제목·본문에 지금 규칙으로 들어 있는지 본다.
- 그대로: 들어 있다.
- 지움: 예전 규칙(부분 문자열)으로만 들어 있고("applauds" 속 "ds") 그 감시의 다른 키워드도 지금 규칙으로 걸리지 않는다.
  회사 감시면 그 회사 연결(document_company WATCH)과 그 연결로 생긴 NEWS 신호(detail.matched_by WATCH)도 지운다
  (TAG·API로 이어진 연결은 둔다).
- 검색어만 고침: 예전 규칙으로만 들어 있지만 같은 감시의 다른 키워드가 지금 규칙으로 걸린다.
- 둠(판정 불가): 검색어가 저장된 글에 아예 없다. 언론 기사는 본문을 저장하지 않으므로 수집 때 본문에서 걸렸거나, 그 뒤 감시 키워드가
  바뀐 기록이다. 근거가 없으니 지우지 않는다. 단 --drop-unverifiable이면 영문 경계 규칙이 걸리는 키워드(라틴 문자·숫자로 시작·끝,
  watchmatch.latin_edged)의 기록은 위 "지움"·"검색어만 고침"처럼 다룬다 — 영문 기사 본문의 "leads" 속 "ds"로 걸린 "DS"처럼 예전 규칙이
  만든 기록일 가능성이 높지만, 본문에 제대로 된 "DS"가 있었던 기록도 함께 지워진다. 한글 키워드("반도체") 기록은 언제나 둔다.
- 더함: 지금 규칙으로 걸리는데 기록이 없다 (--recheck 없이 돌 때와 같다).
네이버에서 받은 문서(문서의 소스 = 그 감시)의 기록과 제외된 감시의 기록은 보지 않는다. 한계: 언론 기사는 제목만 남아 있어, 제목에서
단어 속으로만 걸린 기록은 수집 때 본문에 제대로 된 검색어가 있었더라도 지운다.
--dry-run은 읽기 전용 트랜잭션에서 셈만 하고(지울 기록 일부를 로그에 보인다) 아무것도 쓰지 않는다. 실제 실행도 트랜잭션 하나다.
"""

from __future__ import annotations

import argparse
import logging
from collections import Counter
from dataclasses import dataclass, field

from .config import Settings
from .db import Store
from .watchmatch import contains, from_configs, latin_edged, match_keywords, normalize

log = logging.getLogger("sensing_worker.backfill_watch")

SAMPLES = 30   # --recheck 로그에 보일 지울 기록 수


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
            if _add_hit(store, r["id"], watch_id, keyword):
                added += 1
    return len(rows), added


def _add_hit(store: Store, document_id: int, watch_id: int, keyword: str) -> bool:
    cur = store.conn.execute(
        """
        INSERT INTO document_watch (document_id, source_id, keyword) VALUES (%s, %s, %s)
        ON CONFLICT (document_id, source_id) DO NOTHING RETURNING document_id
        """, (document_id, watch_id, keyword[:200])).fetchone()
    if cur is None:
        return False
    company_id = store.company_of_watch(watch_id)
    if company_id is not None:
        store.add_document_company(document_id, company_id, "WATCH")
    return True


@dataclass
class RecheckResult:
    checked: int = 0                 # 본 문서 수
    removed: int = 0                 # 지운 document_watch
    removed_unverifiable: int = 0    # 그중 근거가 저장된 글에 없던 기록 (--drop-unverifiable)
    rekeyed: int = 0                 # 검색어만 바꾼 기록
    added: int = 0                   # 새로 남긴 기록
    kept: int = 0                    # 판정 불가로 둔 기록 (검색어가 저장된 글에 없음)
    company_links_removed: int = 0   # 지운 document_company (WATCH)
    signals_removed: int = 0         # 지운 company_signal (NEWS, 감시 연결로 생긴 것)
    removed_by_watch: Counter = field(default_factory=Counter)
    samples: list[tuple[int, str, str, bool]] = field(default_factory=list)   # 지울 기록 (감시, 검색어, 제목, 근거 있음) 앞 몇 건


def recheck(store: Store, days: int, dry_run: bool = False, drop_unverifiable: bool = False) -> RecheckResult:
    """최근 days일 동안 수집한 문서의 감시 기록을 지금 규칙으로 다시 판정한다 (모듈 설명 참고). dry_run이면 쓰지 않는다."""
    result = RecheckResult()
    watches = from_configs(store.load_watch_keywords())
    if not watches:
        return result
    watch_ids = [w.source_id for w in watches]
    with store.conn.transaction():
        if dry_run:
            store.conn.execute("SET TRANSACTION READ ONLY")
        docs = store.conn.execute(
            "SELECT id, source_id, title, body_text FROM document "
            "WHERE collected_at >= now() - make_interval(days => %s) ORDER BY id", (days,)).fetchall()
        existing: dict[int, dict[int, str | None]] = {}
        for r in store.conn.execute(
                """
                SELECT w.document_id, w.source_id, w.keyword FROM document_watch w JOIN document d ON d.id = w.document_id
                WHERE d.collected_at >= now() - make_interval(days => %s) AND w.source_id = ANY(%s)
                """, (days, watch_ids)):
            existing.setdefault(r["document_id"], {})[r["source_id"]] = r["keyword"]

        removals: list[tuple[int, int]] = []
        rekeys: list[tuple[int, int, str]] = []
        adds: list[tuple[int, int, str]] = []
        for d in docs:
            result.checked += 1
            rows = existing.get(d["id"], {})
            hits = dict(match_keywords(d["title"], d["body_text"], watches))
            text = normalize(d["title"]) + "\n" + normalize(d["body_text"])
            for watch_id, keyword in rows.items():
                if watch_id == d["source_id"] or not keyword or contains(text, keyword):
                    continue   # 네이버에서 받은 문서, 검색어 없는 기록, 지금 규칙으로도 맞는 기록
                verified = normalize(keyword) in text   # 예전 규칙(부분 문자열)으로는 저장된 글에 있다
                if not verified and not (drop_unverifiable and latin_edged(keyword)):
                    result.kept += 1
                elif watch_id in hits:
                    rekeys.append((d["id"], watch_id, hits[watch_id]))
                else:
                    removals.append((d["id"], watch_id))
                    result.removed_by_watch[watch_id] += 1
                    result.removed_unverifiable += not verified
                    if len(result.samples) < SAMPLES:
                        result.samples.append((watch_id, keyword, (d["title"] or "")[:120], verified))
            adds += [(d["id"], watch_id, keyword) for watch_id, keyword in hits.items() if watch_id not in rows]

        link_removals = _company_links_to_remove(store, removals)
        result.removed, result.rekeyed, result.added = len(removals), len(rekeys), len(adds)
        result.company_links_removed = len(link_removals)
        result.signals_removed = _count_watch_signals(store, link_removals)
        if dry_run:
            return result

        for document_id, watch_id in removals:
            store.conn.execute("DELETE FROM document_watch WHERE document_id = %s AND source_id = %s", (document_id, watch_id))
        for document_id, company_id in link_removals:
            store.conn.execute(
                "DELETE FROM company_signal WHERE company_id = %s AND document_id = %s AND kind = 'NEWS' "
                "AND detail->>'matched_by' = 'WATCH'", (company_id, document_id))
            store.conn.execute(
                "DELETE FROM document_company WHERE document_id = %s AND company_id = %s AND matched_by = 'WATCH'",
                (document_id, company_id))
        for document_id, watch_id, keyword in rekeys:
            store.conn.execute("UPDATE document_watch SET keyword = %s WHERE document_id = %s AND source_id = %s",
                               (keyword[:200], document_id, watch_id))
        result.added = sum(_add_hit(store, document_id, watch_id, keyword) for document_id, watch_id, keyword in adds)
    return result


def _company_links_to_remove(store: Store, removals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """지울 감시 기록 때문에 끊어야 할 (문서, 회사) WATCH 연결. 회사 감시는 회사마다 하나(company.watch_source_id)라
    그 감시 기록이 지워지면 WATCH 연결의 근거가 없다. TAG·API로 이어진 연결은 두고, 보관된 회사의 연결도 함께 정리한다."""
    company_of: dict[int, list[int]] = {}   # 감시 → 회사들
    for r in store.conn.execute("SELECT id, watch_source_id FROM company WHERE watch_source_id IS NOT NULL"):
        company_of.setdefault(r["watch_source_id"], []).append(r["id"])
    candidates = sorted({(document_id, company_id) for document_id, watch_id in removals
                         for company_id in company_of.get(watch_id, [])})
    if not candidates:
        return []
    linked = {(r["document_id"], r["company_id"]) for r in store.conn.execute(
        "SELECT document_id, company_id FROM document_company WHERE matched_by = 'WATCH' AND document_id = ANY(%s)",
        (sorted({c[0] for c in candidates}),))}
    return [c for c in candidates if c in linked]


def _count_watch_signals(store: Store, links: list[tuple[int, int]]) -> int:
    count = 0
    for document_id, company_id in links:
        count += store.conn.execute(
            "SELECT count(*) AS c FROM company_signal WHERE company_id = %s AND document_id = %s AND kind = 'NEWS' "
            "AND detail->>'matched_by' = 'WATCH'", (company_id, document_id)).fetchone()["c"]
    return count


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="이미 수집한 문서에 키워드 감시를 대조한다")
    parser.add_argument("--days", type=int, default=30, help="최근 며칠 동안 수집한 문서를 볼지 (기본 30)")
    parser.add_argument("--recheck", action="store_true",
                        help="이미 남은 기록도 지금 규칙으로 다시 판정해 맞지 않는 것을 지운다 (회사 연결 포함)")
    parser.add_argument("--dry-run", action="store_true", help="--recheck와 함께: 셈만 하고 쓰지 않는다")
    parser.add_argument("--drop-unverifiable", action="store_true",
                        help="--recheck와 함께: 검색어가 저장된 글에 없는 기록도 영문 키워드면 지운다 (본문 미저장 언론 기사)")
    args = parser.parse_args(argv)
    if (args.dry_run or args.drop_unverifiable) and not args.recheck:
        parser.error("--dry-run·--drop-unverifiable은 --recheck와 함께 씁니다")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    store = Store(Settings().db_url)
    try:
        if args.recheck:
            r = recheck(store, args.days, dry_run=args.dry_run, drop_unverifiable=args.drop_unverifiable)
            for watch_id, keyword, title, verified in r.samples:
                log.info("  remove watch=%s keyword=%r title=%r%s", watch_id, keyword, title,
                         "" if verified else " (keyword not in stored text)")
            log.info("watch recheck%s: checked=%s documents, removed=%s %s (unverifiable %s), rekeyed=%s, added=%s, "
                     "kept(unverifiable)=%s, company_links_removed=%s, signals_removed=%s",
                     " (dry run, nothing written)" if args.dry_run else "", r.checked, r.removed,
                     dict(sorted(r.removed_by_watch.items())), r.removed_unverifiable, r.rekeyed, r.added, r.kept,
                     r.company_links_removed, r.signals_removed)
        else:
            checked, added = backfill(store, args.days)
            log.info("watch backfill: checked=%s documents, added=%s hits", checked, added)
    finally:
        store.close()


if __name__ == "__main__":
    main()
