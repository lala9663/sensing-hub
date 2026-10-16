"""작업 실행: ONBOARD(등록 자동 처리)와 CRAWL(주기 수집)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from .apis import ApiConnector, Provider, catalog as api_catalog, make_connector
from .apis import lookup as api_lookup
from .codes import API_KEY_CODES, Code, CrawlFailure, MESSAGES
from .config import Settings
from .db import Job, Store, sha256
from .discovery import (Discoverer, DiscoveryResult, canonical_article_url, looks_like_feed, looks_like_sitemap,
                        matches, parse_feed_entries, same_site, sitemap_urls)
from .evidence import EvidenceStore
from .extract import extract_article, links_with_dates
from .fetcher import EVIDENCE_CHARS, PoliteFetcher
from .llm import LlmClient, LlmUnavailable, NoLlm, make_llm
from .policy import defaults_for, guess_site_type, passes_quality
from .recipe import propose_recipe

LLM_STEP_MESSAGE = "사이트 구조를 분석해 수집 방법을 만드는 중입니다."

log = logging.getLogger(__name__)


@dataclass
class CrawlStats:
    found: int = 0
    new: int = 0
    updated: int = 0
    errors: int = 0
    field_missing: int = 0
    anomalies: int = 0


class JobRunner:
    def __init__(self, store: Store, fetcher: PoliteFetcher, settings: Settings,
                 evidence: EvidenceStore | None = None, llm: LlmClient | None = None):
        self.db = store
        self.f = fetcher
        self.s = settings
        self.evidence = evidence or EvidenceStore(settings.evidence_dir, settings.evidence_retention_days)
        # 등록 시 수집 레시피를 쓰는 LLM. 설정이 없으면 NoLlm → 휴리스틱 탐색만 한다
        self.llm = llm if llm is not None else make_llm(settings)

    def _record(self, job: Job, failure: CrawlFailure) -> None:
        """실패를 기록한다. 응답 본문이 붙어 있으면 증거 파일로 남기고 경로를 함께 적는다."""
        self.db.record_error(job.id, failure, self.evidence.save(job.id, failure))

    def run(self, job: Job) -> str:
        source = self.db.load_source(job.source_id)
        try:
            if job.job_type == "ONBOARD":
                return self._onboard(job, source)
            if job.job_type in ("CRAWL", "BACKFILL"):
                return self._crawl_job(job, source)
            raise CrawlFailure(Code.UNEXPECTED, f"지원하지 않는 작업 유형: {job.job_type}")
        except CrawlFailure as failure:
            self._record(job, failure)
            self._apply_failure(job, source, failure)
            self.db.finish_job(job.id, "FAILED", errors=1)
            return "FAILED"

    # ------------------------------------------------------------------ ONBOARD
    def _onboard(self, job: Job, source: dict) -> str:
        """사전 점검 → 자동 탐색 → 품질 검사 → 기본값 적용(설정 새 버전) → 첫 수집."""
        base_url, domain = source["base_url"], source["domain"]

        # 0) 공개 API가 있는 제공처(DART 등)는 크롤링하지 않고 API 소스로 등록한다
        provider = api_lookup(domain)
        if provider is not None:
            return self._onboard_api(job, source, provider)

        # 1) 사전 점검
        self.db.update_registration(job.source_id, step="PROBE", message="사전 점검 중입니다.")
        probe = probe_site(self.f, domain, base_url)
        site_type = probe.site_type
        self.db.update_site(source["site_id"], robots_status=probe.robots, site_type=site_type,
                            ai_restricted=probe.ai_blocked, risk_flags={"ai_bots_blocked": probe.ai_blocked})

        # 2) 자동 탐색 + 3) 품질 검사: 후보를 유력한 순서로 샘플 검사해 첫 합격을 쓴다.
        #    LLM이 설정돼 있으면 홈·샘플 페이지로 레시피를 쓰게 하고, 같은 품질 검사를 통과하면 그것을 쓴다
        self.db.update_registration(job.source_id, step="DISCOVERY",
                                    message="기사 입구와 주소 패턴을 찾고 샘플 기사로 품질을 검사하는 중입니다.")
        result, detail = discover(
            self.f, domain, base_url, site_type, llm=self.llm, settings=self.s,
            progress=lambda msg: self.db.update_registration(job.source_id, step="DISCOVERY", message=msg))

        # 4) 기본값 적용 → 설정 새 버전 → ACTIVE (레시피의 selectors·recipe_source도 설정에 들어간다)
        defaults = defaults_for(site_type)
        config = {**result.to_config(), "store_scope": defaults["store_scope"],
                  "collect_attachments": defaults["collect_attachments"]}
        first = source["config_version"] is None
        if result.recipe_source == "llm":
            note = "LLM 레시피로 생성" if first else "LLM 레시피 다시 생성"
        else:
            note = "자동 탐색으로 생성" if first else "자동 탐색 다시 실행"
        config_id, version = self.db.activate_new_config(job.source_id, config, f"{note} ({detail})"[:500],
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
        log.info("onboard ok source=%s domain=%s method=%s recipe=%s v%s new=%s", job.source_id, domain,
                 result.method, result.recipe_source, version, stats.new)
        return status

    # ------------------------------------------------------------------ ONBOARD (공개 API)
    def _onboard_api(self, job: Job, source: dict, provider: Provider) -> str:
        """카탈로그에 있는 제공처: 키 확인 → 시험 호출 → 설정(method API) 활성화 → 첫 수집.

        키가 없으면 REVIEW/API_KEY_REQUIRED로 멈춘다 (관리자가 키를 넣으면 백엔드가 ONBOARD를 다시 넣는다).
        키가 틀리면 자격 증명을 INVALID로 표시하고 REVIEW/API_KEY_INVALID로 멈춘다.
        키워드 감시(source.kind WATCH)는 백엔드가 만든 활성 설정(keywords)을 그대로 쓰고 새 버전을 만들지 않는다.
        키워드 제공자(네이버 뉴스)의 도메인을 사이트처럼 등록하면 REVIEW/KEYWORDS_REQUIRED로 멈춘다.
        """
        watch = source.get("kind") == "WATCH"
        self.db.update_site(source["site_id"], site_type="NEWS" if provider.keyword_based else "PUBLIC",
                            risk_flags={"api_provider": provider.id})
        if provider.keyword_based and not watch:
            raise CrawlFailure(Code.KEYWORDS_REQUIRED,
                               f"{provider.name.replace(' 검색', '')}는 키워드 감시로 등록합니다. "
                               "관리자 또는 등록 담당자가 키워드 묶음을 만들어 주세요.", stage="PROBE")
        self.db.update_registration(job.source_id, step="API_KEY", message=f"{provider.name} API 키를 확인하는 중입니다.")
        secret = self._load_secret(provider.id, provider, stage="PROBE")
        connector = make_connector(provider.id, self.f)
        try:
            connector.verify(secret)
        except CrawlFailure as e:
            e.stage = "PROBE"
            if e.code == Code.API_KEY_INVALID:
                self.db.mark_credential(provider.id, "INVALID", str(e))
            raise
        self.db.mark_credential(provider.id, "VALID", None)

        # 공시·공공 API는 6시간, 키워드 감시(보도 모니터링)는 60분 주기
        interval = 60 if provider.keyword_based else defaults_for("PUBLIC")["interval_minutes"]
        if watch and source.get("config"):
            config = source["config"]
            config_id = source["active_config_version_id"]
            version = source["config_version"]
            self.db.activate_source(job.source_id, config.get("method", "API"), interval)
        else:
            config = connector.default_config()
            first = source["config_version"] is None
            config_id, version = self.db.activate_new_config(
                job.source_id, config, f"{provider.name} API로 {'생성' if first else '다시 생성'} (키 검증 통과)", interval)

        self.db.update_registration(job.source_id, step="FIRST_CRAWL", message="첫 수집 중입니다.")
        stats = self._crawl_api(job, source, config, connector=connector, secret=secret)
        self.db.mark_crawl_result(job.source_id, True, interval)
        status = "PARTIAL" if stats.errors else "SUCCESS"
        self.db.finish_job(job.id, status, stats.found, stats.new, stats.errors, config_id)
        what = "감시 등록 완료" if watch else "등록 완료"
        self.db.update_registration(
            job.source_id, status="COMPLETED", step="DONE",
            message=f"{what}. {provider.name} API로 {stats.new}건을 받았고 {interval}분마다 업데이트합니다.")
        log.info("onboard ok source=%s provider=%s kind=%s method=API v%s new=%s", job.source_id, provider.id,
                 source.get("kind", "SITE"), version, stats.new)
        return status

    def _load_secret(self, provider_id: str, provider: Provider | None, stage: str) -> str:
        """복호화한 키. 대칭키나 자격 증명이 없으면 API_KEY_REQUIRED (사용자 메시지는 제공자의 발급 안내)."""
        hint = provider.key_required_message if provider else MESSAGES[Code.API_KEY_REQUIRED]
        if not self.s.secret_key:
            raise CrawlFailure(Code.API_KEY_REQUIRED, f"{hint} (워커에 SENSING_SECRET_KEY가 설정되지 않았습니다.)",
                               stage=stage)
        secret = self.db.load_credential(provider_id, self.s.secret_key)
        if not secret:
            raise CrawlFailure(Code.API_KEY_REQUIRED, hint, stage=stage)
        return secret

    # ------------------------------------------------------------------ CRAWL
    def _crawl_job(self, job: Job, source: dict) -> str:
        if not source.get("config"):
            raise CrawlFailure(Code.DISCOVERY_FAILED, "활성 설정이 없습니다.", stage="LIST")
        if source["config"].get("method") == "API":
            stats = self._crawl_api(job, source, source["config"])
        else:
            stats = self._crawl(job, source, source["config"])
        streak = self.db.mark_anomaly(job.source_id, stats.anomalies > 0)
        if streak >= self.s.anomaly_broken_after:
            # 조용한 실패가 이어진다: 사이트 개편으로 일부만 잡히는 것으로 보고 관리자에게 넘긴다
            self.db.set_source_status(job.source_id, "BROKEN", Code.ANOMALY_DROP.value)
            log.warning("source %s BROKEN: %s consecutive anomalies", job.source_id, streak)
        elif streak == 0 and source["status"] in ("BLOCKED", "BROKEN"):  # 관리자 재점검 통과
            self.db.set_source_status(job.source_id, "ACTIVE")
        self.db.mark_crawl_result(job.source_id, True, source["interval_minutes"])
        status = "PARTIAL" if stats.errors else "SUCCESS"
        self.db.finish_job(job.id, status, stats.found, stats.new, stats.errors,
                           source["active_config_version_id"])
        return status

    def _crawl(self, job: Job, source: dict, config: dict) -> CrawlStats:
        stats = CrawlStats()
        pattern = config["article_pattern"]
        domain = config.get("site_domain") or source["domain"]

        # 허브에서 기사 후보 모으기
        candidates: list[str] = []
        hints: dict[str, str] = {}  # 기사 URL → 목록·피드에 보인 날짜 (상세에 날짜가 없을 때 씀)
        first_hub_html: str | None = None
        opened = 0  # 실제로 연 허브 수
        hub_failures: list[CrawlFailure] = []  # 못 연 허브의 비차단 오류 (섹션 하나가 404여도 수집은 계속한다)
        for hub in config["hubs"]:
            try:
                page = self.f.get(hub)
            except CrawlFailure as e:
                e.stage = "LIST"
                if e.blocking:
                    # 차단 신호는 즉시 중단. 앞서 못 연 허브의 오류는 기록만 남긴다 (실패 자체는 run()이 기록)
                    for hf in hub_failures:
                        self._record(job, hf)
                    raise
                log.warning("hub skipped source=%s url=%s code=%s", job.source_id, hub, e.code.value)
                hub_failures.append(e)
                continue
            opened += 1
            first_hub_html = first_hub_html if first_hub_html is not None else page.text
            if looks_like_feed(page.text):
                entries = parse_feed_entries(page.text)
            elif looks_like_sitemap(page.text):
                entries = [(u, None) for u in sitemap_urls(self.f.get, page.text)[: self.s.max_articles_per_run * 3]]
            else:
                entries = links_with_dates(page.text, page.url)
            for u, date in entries:
                if same_site(u, domain) and matches(u, pattern):
                    c = canonical_article_url(u, pattern)
                    candidates.append(c)
                    if date and c not in hints:
                        hints[c] = date
        if not opened and hub_failures:
            # 허브를 하나도 못 열었다 → 마지막 오류로 실패 처리 (run()이 기록·반영). 앞의 오류는 여기서 남긴다
            for hf in hub_failures[:-1]:
                self._record(job, hf)
            raise hub_failures[-1]
        for hf in hub_failures:
            self._record(job, hf)
            stats.errors += 1
        candidates = list(dict.fromkeys(candidates))
        stats.found = len(candidates)

        hub_evidence = (first_hub_html or "")[:EVIDENCE_CHARS] or None
        if not candidates:
            raise CrawlFailure(Code.PARSE_EMPTY, url=config["hubs"][0] if config["hubs"] else None, stage="LIST",
                               evidence=hub_evidence)

        # 조용한 실패 감지 1: 허브에 보이는 기사 후보가 평소보다 크게 줄었다 (일부 섹션만 개편 등)
        baseline = self.db.found_baseline(job.source_id, source.get("active_config_version_id"))
        if baseline and stats.found < baseline * self.s.anomaly_drop_ratio:
            self._record(job, CrawlFailure(
                Code.ANOMALY_DROP, f"기사 후보가 {stats.found}건으로 평소({baseline:.1f}건)보다 크게 줄었습니다.",
                url=config["hubs"][0], stage="LIST", evidence=hub_evidence))
            stats.errors += 1
            stats.anomalies += 1

        hashes = {sha256(u): u for u in candidates}
        known = self.db.known_urls(list(hashes))
        fresh = [u for h, u in hashes.items() if h not in known][: self.s.max_articles_per_run]

        scope = config.get("store_scope", "FULL")
        missing_sample: tuple[str, str] | None = None  # 제목·발행일을 못 찾은 첫 페이지 (증거용)
        for url in fresh:
            try:
                page = self.f.get(url)
            except CrawlFailure as e:
                if e.blocking:
                    raise
                e.stage = "DETAIL"
                self._record(job, e)
                stats.errors += 1
                continue
            art = extract_article(page.text, page.url, config.get("selectors"), date_hint=hints.get(url),
                                  date_format=config.get("date_format"))
            if not art.title or not art.published_at:
                stats.field_missing += 1
                missing_sample = missing_sample or (page.url, page.text[:EVIDENCE_CHARS])
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

        # 조용한 실패 감지 2: 필드가 절반 넘게 비면 상세 페이지 구조 변경 의심
        if fresh and stats.field_missing > len(fresh) / 2:
            self._record(job, CrawlFailure(
                Code.ANOMALY_DROP, f"새 기사 {len(fresh)}건 중 {stats.field_missing}건에서 제목·발행일을 찾지 못했습니다.",
                url=missing_sample[0], stage="EXTRACT", evidence=missing_sample[1]))
            stats.errors += 1
            stats.anomalies += 1
        return stats

    def _crawl_api(self, job: Job, source: dict, config: dict, *, connector: ApiConnector | None = None,
                   secret: str | None = None) -> CrawlStats:
        """공개 API 수집: 허브 루프 대신 커넥터 fetch. 급감 감지는 크롤링과 같이 found_count 기준이다.

        0건은 실패가 아니다 (주말·휴일 공시처럼 정상적으로 비는 날이 있다) — PARSE_EMPTY를 내지 않는다.
        """
        stats = CrawlStats()
        provider_id = config["provider"]
        if secret is None:
            secret = self._load_secret(provider_id, api_catalog.get(provider_id), stage="LIST")
        connector = connector or make_connector(provider_id, self.f)
        since = (datetime.now(timezone.utc) - timedelta(days=max(0, int(config.get("days", 1))))).date()
        try:
            items = connector.fetch(secret, config, since, self.s.max_api_items_per_run)
        except CrawlFailure as e:
            if e.code == Code.API_KEY_INVALID:
                self.db.mark_credential(provider_id, "INVALID", str(e))
            raise
        stats.found = len(items)

        baseline = self.db.found_baseline(job.source_id, source.get("active_config_version_id"))
        if baseline and stats.found < baseline * self.s.anomaly_drop_ratio:
            self._record(job, CrawlFailure(
                Code.ANOMALY_DROP, f"API 항목이 {stats.found}건으로 평소({baseline:.1f}건)보다 크게 줄었습니다.", stage="LIST"))
            stats.errors += 1
            stats.anomalies += 1

        for item in items:
            doc_id, result = self.db.upsert_document(
                source_id=job.source_id, job_id=job.id, url=item.url, title=item.title, author=item.author,
                section=item.section, published_at=item.published_at, body=item.body)
            if result == "NEW":
                stats.new += 1
            elif result == "UPDATED":
                stats.updated += 1
            if item.keyword:
                # 같은 기사가 다른 감시에서도 잡히면 문서는 하나, 감시별 기록은 각각 (document_watch)
                self.db.add_watch_hit(doc_id, job.source_id, item.keyword)
            for att_url, name in item.attachments:
                self.db.add_attachment(doc_id, att_url, name)
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
        elif code in API_KEY_CODES or code == Code.KEYWORDS_REQUIRED:
            # 키가 사라졌거나 만료됐다(관리자 큐 API_KEY, 키를 다시 넣으면 백엔드가 ONBOARD를 넣는다),
            # 또는 감시의 키워드가 모두 지워졌다(REVIEW)
            self.db.set_source_status(job.source_id, "REVIEW", code.value)
        self.db.mark_crawl_result(job.source_id, False, source["interval_minutes"])


# ---------------------------------------------------------------------- 온보딩 판단 (DB 없이도 쓴다: evaluate.py)

@dataclass
class Probe:
    robots: str
    ai_blocked: bool
    site_type: str


def probe_site(f: PoliteFetcher, domain: str, base_url: str) -> Probe:
    """사전 점검: robots·AI 봇 차단 여부, 홈 접속(차단·로그인 판정 포함), 사이트 유형."""
    robots = f.robots_status(base_url)
    ai_blocked = f.ai_bots_blocked(base_url)
    if robots == "DISALLOWED":
        raise CrawlFailure(Code.ROBOTS_DISALLOWED, url=base_url, stage="PROBE")
    home = f.get(base_url)
    return Probe(robots, ai_blocked, guess_site_type(domain, home.text))


def check_quality(f: PoliteFetcher, result: DiscoveryResult, base_url: str, site_type: str = "ETC") -> str:
    """샘플 5건으로 품질 검사. 통과하면 설명을 돌려주고, 아니면 QUALITY_FAILED.

    언론사가 아니면 본문 대신 첨부(보도자료 HWP·PDF)가 있어도 샘플로 인정한다.
    후보에 셀렉터(LLM 레시피)가 있으면 수집 때와 똑같이 그 셀렉터로 뽑아 검사한다.
    """
    samples = []
    for url in result.sample_urls[:5]:
        try:
            page = f.get(url)
            samples.append(extract_article(page.text, page.url, result.selectors, date_hint=result.sample_dates.get(url),
                                           date_format=result.date_format))
        except CrawlFailure as e:
            if e.blocking:
                raise
    ok, detail = passes_quality(samples, attachments_ok=site_type != "NEWS")
    if not ok:
        raise CrawlFailure(Code.QUALITY_FAILED, f"{MESSAGES[Code.QUALITY_FAILED]} ({detail})",
                           url=base_url, stage="DISCOVERY")
    return detail


def discover(f: PoliteFetcher, domain: str, base_url: str, site_type: str, max_tries: int = 6,
             llm: LlmClient | None = None, settings: Settings | None = None,
             progress: Callable[[str], None] | None = None) -> tuple[DiscoveryResult, str]:
    """자동 탐색 후보를 유력한 순서로 품질 검사해 첫 합격 (결과, 검사 설명)을 돌려준다.

    LLM이 설정돼 있으면(NoLlm이 아니면) 휴리스틱 결과와 관계없이 레시피 생성을 한 번 시도하고, 그 레시피가 같은
    품질 검사를 통과하면 그것을 쓴다. 아니면 휴리스틱 합격 후보로 돌아간다. LLM 장애(LlmUnavailable)는 경고만
    남기고 휴리스틱으로 진행한다 — LLM이 멈췄다고 등록이 실패해서는 안 된다.
    둘 다 없으면: 후보가 하나도 없었으면 DISCOVERY_FAILED, 있었지만 모두 불합격이면 첫 후보의 QUALITY_FAILED.
    """
    tried: set[str] = set()
    first_failure: CrawlFailure | None = None
    seen: list[DiscoveryResult] = []
    passed: tuple[DiscoveryResult, str] | None = None
    disc = Discoverer(f, domain, base_url)
    for result in disc.candidates():
        sig = result.article_pattern.get("signature") or result.article_pattern["path"]
        if sig in tried:
            continue
        tried.add(sig)
        seen.append(result)
        try:
            passed = (result, check_quality(f, result, base_url, site_type))
            break
        except CrawlFailure as e:
            if e.code != Code.QUALITY_FAILED:
                raise
            log.info("discovery candidate rejected domain=%s method=%s pattern=%s: %s", domain, result.method, sig, e)
            first_failure = first_failure or e
        if len(tried) >= max_tries:
            break

    if llm is not None and not isinstance(llm, NoLlm):
        if progress:
            progress(LLM_STEP_MESSAGE)
        try:
            outcome = propose_recipe(f, llm, settings or Settings(), disc, seen, site_type,
                                     quality=lambda r: check_quality(f, r, base_url, site_type))
        except LlmUnavailable as e:
            log.warning("LLM unavailable, continuing with heuristics domain=%s: %s", domain, e)
        else:
            if outcome.result is not None:
                log.info("llm recipe accepted domain=%s pattern=%s hubs=%s selectors=%s", domain,
                         outcome.result.article_pattern.get("signature"), len(outcome.result.hubs),
                         sorted(outcome.result.selectors))
                return outcome.result, outcome.detail
            log.info("llm recipe rejected domain=%s: %s", domain, outcome.detail)

    if passed:
        return passed
    if first_failure:
        raise CrawlFailure(Code.QUALITY_FAILED, f"{first_failure} [후보 {len(tried)}개 검사]",
                           url=base_url, stage="DISCOVERY")
    raise CrawlFailure(Code.DISCOVERY_FAILED, stage="DISCOVERY", url=base_url)


def host_of(url: str) -> str:
    return urlsplit(url).hostname or ""

