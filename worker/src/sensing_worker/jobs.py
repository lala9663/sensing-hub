"""작업 실행: ONBOARD(등록 자동 처리)와 CRAWL(주기 수집)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

from .codes import Code, CrawlFailure, MESSAGES
from .config import Settings
from .db import Job, Store, sha256
from .discovery import Discoverer, canonical_article_url, looks_like_feed, matches, parse_feed, parse_sitemap, same_site
from .extract import extract_article, links
from .fetcher import PoliteFetcher
from .policy import defaults_for, guess_site_type, passes_quality

log = logging.getLogger(__name__)


@dataclass
class CrawlStats:
    found: int = 0
    new: int = 0
    updated: int = 0
    errors: int = 0
    field_missing: int = 0


class JobRunner:
    def __init__(self, store: Store, fetcher: PoliteFetcher, settings: Settings):
        self.db = store
        self.f = fetcher
        self.s = settings

    def run(self, job: Job) -> str:
        source = self.db.load_source(job.source_id)
        try:
            if job.job_type == "ONBOARD":
                return self._onboard(job, source)
            if job.job_type in ("CRAWL", "BACKFILL"):
                return self._crawl_job(job, source)
            raise CrawlFailure(Code.UNEXPECTED, f"지원하지 않는 작업 유형: {job.job_type}")
        except CrawlFailure as failure:
            self.db.record_error(job.id, failure)
            self._apply_failure(job, source, failure)
            self.db.finish_job(job.id, "FAILED", errors=1)
            return "FAILED"

    # ------------------------------------------------------------------ ONBOARD
    def _onboard(self, job: Job, source: dict) -> str:
        """사전 점검 → 자동 탐색 → 품질 검사 → 기본값 적용(설정 새 버전) → 첫 수집."""
        base_url, domain = source["base_url"], source["domain"]

        # 1) 사전 점검
        self.db.update_registration(job.source_id, step="PROBE", message="사전 점검 중입니다.")
        robots = self.f.robots_status(base_url)
        ai_blocked = self.f.ai_bots_blocked(base_url)
        if robots == "DISALLOWED":
            raise CrawlFailure(Code.ROBOTS_DISALLOWED, url=base_url, stage="PROBE")
        home = self.f.get(base_url)
        site_type = guess_site_type(domain, home.text)
        self.db.update_site(source["site_id"], robots_status=robots, site_type=site_type, ai_restricted=ai_blocked,
                            risk_flags={"ai_bots_blocked": ai_blocked})

        # 2) 자동 탐색
        self.db.update_registration(job.source_id, step="DISCOVERY", message="기사 입구와 기사 주소 패턴을 찾는 중입니다.")
        result = Discoverer(self.f, domain, base_url).run()

        # 3) 품질 검사 (샘플 5건)
        self.db.update_registration(job.source_id, step="QUALITY", message="샘플 기사로 품질을 검사하는 중입니다.")
        samples = []
        for url in result.sample_urls[:5]:
            try:
                page = self.f.get(url)
                samples.append(extract_article(page.text, page.url))
            except CrawlFailure as e:
                if e.blocking:
                    raise
        ok, detail = passes_quality(samples)
        if not ok:
            raise CrawlFailure(Code.QUALITY_FAILED, f"{MESSAGES[Code.QUALITY_FAILED]} ({detail})",
                               url=base_url, stage="DISCOVERY")

        # 4) 기본값 적용 → 설정 새 버전 → ACTIVE
        defaults = defaults_for(site_type)
        config = {**result.to_config(), "store_scope": defaults["store_scope"],
                  "collect_attachments": defaults["collect_attachments"], "selectors": {}}
        note = "자동 탐색으로 생성" if source["config_version"] is None else "자동 탐색 다시 실행"
        config_id, version = self.db.activate_new_config(job.source_id, config, f"{note} ({detail})",
                                                         defaults["interval_minutes"])

        # 5) 첫 수집: 허브(피드·목록 첫 페이지)에 보이는 최신 기사만 가져온다
        self.db.update_registration(job.source_id, step="FIRST_CRAWL", message="첫 수집 중입니다.")
        stats = self._crawl(job, source, config)
        self.db.mark_crawl_result(job.source_id, True, defaults["interval_minutes"])
        status = "PARTIAL" if stats.errors else "SUCCESS"
        self.db.finish_job(job.id, status, stats.found, stats.new, stats.errors, config_id)
        self.db.update_registration(
            job.source_id, status="COMPLETED", step="DONE",
            message=f"등록 완료. 기사 {stats.new}건을 수집했고 {defaults['interval_minutes']}분마다 업데이트합니다."
                    + (" 언론사로 분류되어 제목·링크·발행일만 저장합니다." if defaults["store_scope"] == "TITLE_LINK" else ""))
        log.info("onboard ok source=%s domain=%s method=%s v%s new=%s", job.source_id, domain, result.method,
                 version, stats.new)
        return status

    # ------------------------------------------------------------------ CRAWL
    def _crawl_job(self, job: Job, source: dict) -> str:
        if not source.get("config"):
            raise CrawlFailure(Code.DISCOVERY_FAILED, "활성 설정이 없습니다.", stage="LIST")
        stats = self._crawl(job, source, source["config"])
        if source["status"] in ("BLOCKED", "BROKEN"):  # 관리자 재점검 통과
            self.db.set_source_status(job.source_id, "ACTIVE")
        self.db.mark_crawl_result(job.source_id, True, source["interval_minutes"])
        status = "PARTIAL" if stats.errors else "SUCCESS"
        self.db.finish_job(job.id, status, stats.found, stats.new, stats.errors)
        return status

    def _crawl(self, job: Job, source: dict, config: dict) -> CrawlStats:
        stats = CrawlStats()
        pattern = config["article_pattern"]
        domain = source["domain"]

        # 허브에서 기사 후보 모으기
        candidates: list[str] = []
        for hub in config["hubs"]:
            page = self.f.get(hub)
            if looks_like_feed(page.text):
                urls = parse_feed(page.text)
            elif page.text.lstrip().startswith("<?xml") and "<urlset" in page.text[:2000]:
                urls = parse_sitemap(page.text)[0]
            else:
                urls = links(page.text, page.url)
            candidates += [canonical_article_url(u, pattern) for u in urls
                           if same_site(u, domain) and matches(u, pattern)]
        candidates = list(dict.fromkeys(candidates))
        stats.found = len(candidates)

        if not candidates:
            raise CrawlFailure(Code.PARSE_EMPTY, url=config["hubs"][0] if config["hubs"] else None, stage="LIST")

        hashes = {sha256(u): u for u in candidates}
        known = self.db.known_urls(list(hashes))
        fresh = [u for h, u in hashes.items() if h not in known][: self.s.max_articles_per_run]

        scope = config.get("store_scope", "FULL")
        for url in fresh:
            try:
                page = self.f.get(url)
            except CrawlFailure as e:
                if e.blocking:
                    raise
                e.stage = "DETAIL"
                self.db.record_error(job.id, e)
                stats.errors += 1
                continue
            art = extract_article(page.text, page.url, config.get("selectors"))
            if not art.title or not art.published_at:
                stats.field_missing += 1
            doc_id, result = self.db.upsert_document(
                source_id=job.source_id, job_id=job.id, url=url, title=art.title, author=art.author,
                section=art.section, published_at=art.published_at,
                body=None if scope == "TITLE_LINK" else art.body)
            if result == "NEW":
                stats.new += 1
            elif result == "UPDATED":
                stats.updated += 1
            if config.get("collect_attachments"):
                for att_url, name in art.attachments:
                    self.db.add_attachment(doc_id, att_url, name)

        # 조용한 실패 감지: 필드가 절반 넘게 비면 구조 변경 의심
        if fresh and stats.field_missing > len(fresh) / 2:
            self.db.record_error(job.id, CrawlFailure(
                Code.ANOMALY_DROP, f"새 기사 {len(fresh)}건 중 {stats.field_missing}건에서 제목·발행일을 찾지 못했습니다.",
                url=config["hubs"][0], stage="EXTRACT"))
            stats.errors += 1
        return stats

    # ------------------------------------------------------------------ 실패 처리
    def _apply_failure(self, job: Job, source: dict, failure: CrawlFailure) -> None:
        code = failure.code
        message = str(failure)
        if job.job_type == "ONBOARD":
            if code in (Code.DISCOVERY_FAILED, Code.QUALITY_FAILED):
                self.db.set_source_status(job.source_id, "REVIEW", code.value)
                self.db.update_registration(job.source_id, status="REVIEW", step="REVIEW", message=message)
            elif failure.blocking or code == Code.ROBOTS_DISALLOWED:
                self.db.set_source_status(job.source_id, "BLOCKED", code.value)
                self.db.update_registration(job.source_id, status="REJECTED", step="REJECTED",
                                            message=f"등록할 수 없습니다: {message}")
            else:
                self.db.set_source_status(job.source_id, "REVIEW", code.value)
                self.db.update_registration(job.source_id, status="REVIEW", step="REVIEW", message=message)
            return

        # 주기 수집 중 실패
        if failure.blocking:
            self.db.set_source_status(job.source_id, "BLOCKED", code.value)
        elif code in (Code.PARSE_EMPTY, Code.DISCOVERY_FAILED):
            self.db.set_source_status(job.source_id, "BROKEN", code.value)
        self.db.mark_crawl_result(job.source_id, False, source["interval_minutes"])


def host_of(url: str) -> str:
    return urlsplit(url).hostname or ""
