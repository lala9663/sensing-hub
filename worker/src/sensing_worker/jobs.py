"""작업 실행: ONBOARD(등록 자동 처리)와 CRAWL(주기 수집). 점검 소스(kind CHECK)의 CRAWL은 회사 단위 상태 확인이다."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from .apis import ApiConnector, Provider, catalog as api_catalog, make_check_connector, make_connector
from .apis import lookup as api_lookup
from .codes import API_KEY_CODES, Code, CrawlFailure, MESSAGES
from .company import biz_status_signal, build_corp_index, debarment_signal, disclosure_signal, pick_corp_code
from .config import Settings
from .db import Job, Store, sha256
from .discovery import (Discoverer, DiscoveryResult, canonical_article_url, looks_like_feed, looks_like_sitemap,
                        matches, parse_feed_entries, same_site, sitemap_urls)
from .evidence import EvidenceStore
from .extract import extract_article, links_with_dates
from .fetcher import EVIDENCE_CHARS, Page, PoliteFetcher
from .llm import LlmClient, LlmUnavailable, NoLlm, make_llm
from . import portal
from .policy import defaults_for, guess_site_type, next_interval, passes_quality
from .recipe import propose_recipe

LLM_STEP_MESSAGE = "사이트 구조를 분석해 수집 방법을 만드는 중입니다."
CHECK_INTERVAL_MINUTES = 1440   # 회사 단위 점검의 기본 주기 (하루 1회)
# 점검 종류별 주기 (백엔드가 소스에 정한 값이 있으면 그것을 쓴다): 사업자 상태 매일, 고유번호 매핑 주 1회, 회사별 공시 6시간, 부정당업자 매일
CHECK_INTERVALS = {"nts_status": 1440, "dart_corp_map": 10080, "dart_disclosure": 360, "g2b_debarment": 1440}
DISCLOSURE_DEFAULT_DAYS = 3

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
        try:
            result, detail = discover(
                self.f, domain, base_url, site_type, llm=self.llm, settings=self.s,
                progress=lambda msg: self.db.update_registration(job.source_id, step="DISCOVERY", message=msg))
        except CrawlFailure as e:
            # 기사를 못 찾은 페이지가 다른 언론사로 가는 링크 모음(뉴스스탠드 등)이면, 실패시키지 않고
            # 가리키는 언론사를 대신 등록한다. 사전 점검에서 받은 홈을 다시 써서 요청을 더 보내지 않는다
            if e.code not in (Code.DISCOVERY_FAILED, Code.QUALITY_FAILED):
                raise
            outlets = portal.detect(probe.home.text, probe.home.url, domain)
            if not outlets:
                raise
            return self._expand_portal(job, source, outlets)

        # 4) 기본값 적용 → 설정 새 버전 → ACTIVE (레시피의 selectors·recipe_source도 설정에 들어간다)
        defaults = defaults_for(site_type)
        config = {**result.to_config(), "store_scope": defaults["store_scope"],
                  "collect_attachments": defaults["collect_attachments"]}
        first = source["config_version"] is None
        if result.recipe_source == "llm":
            note = "LLM 레시피로 생성" if first else "LLM 레시피 다시 생성"
        else:
            note = "자동 탐색으로 생성" if first else "자동 탐색 다시 실행"
        # 사람이 소스 상세에서 정한 주기(interval_locked)는 재탐색해도 그대로 둔다
        interval = int(source["interval_minutes"]) if source.get("interval_locked") else defaults["interval_minutes"]
        config_id, version = self.db.activate_new_config(job.source_id, config, f"{note} ({detail})"[:500], interval)

        # 5) 첫 수집: 허브(피드·목록 첫 페이지)에 보이는 최신 기사만 가져온다
        self.db.update_registration(job.source_id, step="FIRST_CRAWL", message="첫 수집 중입니다.")
        stats = self._crawl(job, source, config)
        self.db.mark_crawl_result(job.source_id, True, interval)
        status = "PARTIAL" if stats.errors else "SUCCESS"
        self.db.finish_job(job.id, status, stats.found, stats.new, stats.errors, config_id)
        self.db.update_registration(
            job.source_id, status="COMPLETED", step="DONE",
            message=f"등록 완료. 기사 {stats.new}건을 수집했고 {interval}분마다 업데이트합니다."
                    + (" 언론사로 분류되어 제목·링크·발행일만 저장합니다." if defaults["store_scope"] == "TITLE_LINK" else ""))
        log.info("onboard ok source=%s domain=%s method=%s recipe=%s v%s new=%s", job.source_id, domain,
                 result.method, result.recipe_source, version, stats.new)
        return status

    # ------------------------------------------------------------------ ONBOARD (링크 모음 페이지 → 언론사 확장)
    def _expand_portal(self, job: Job, source: dict, outlets: list[portal.Outlet]) -> str:
        """모음 페이지가 가리키는 언론사를 요청자 이름으로 등록하고, 모음 페이지 소스는 EXCLUDED/PORTAL_EXPANDED로 둔다.

        언론사마다 백엔드 등록과 같은 행(사이트·소스 PENDING·ONBOARD 작업·등록 요청)을 만들며 batch_id는 portal:<소스 id>라
        화면이 묶음 진행(/registrations/batches/portal:<id>)으로 언론사들의 진행을 보여 준다. 이미 열린 소스가 있는 도메인과
        상한(Settings.portal_max_outlets)을 넘는 것은 건너뛰고 로그에 남긴다.
        """
        reg = self.db.load_registration(job.source_id)
        user_id = reg["user_id"] if reg else source.get("requested_by")
        channel = reg["channel"] if reg else "WEB"
        batch_id = portal_batch_id(job.source_id)
        cap = max(0, self.s.portal_max_outlets)
        registered: list[str] = []
        duplicates: list[str] = []
        for outlet in outlets[:cap]:
            new_id = self.db.register_outlet(user_id=user_id, channel=channel, input_url=outlet.base_url,
                                             domain=outlet.domain, base_url=outlet.base_url, name=outlet.name,
                                             batch_id=batch_id)
            (registered if new_id is not None else duplicates).append(outlet.domain)
        over_cap = [o.domain for o in outlets[cap:]]

        message = portal_message(len(registered), batch_id, len(duplicates), len(over_cap))
        self.db.set_source_status(job.source_id, "EXCLUDED", Code.PORTAL_EXPANDED.value)
        self.db.update_registration(job.source_id, status="COMPLETED", step="DONE", message=message)
        self.db.finish_job(job.id, "SUCCESS", found=len(outlets), new=len(registered))
        log.info("portal expanded source=%s domain=%s batch=%s registered=%s skipped_duplicate=%s skipped_cap=%s",
                 job.source_id, source["domain"], batch_id, registered, duplicates, over_cap)
        return "SUCCESS"

    # ------------------------------------------------------------------ ONBOARD (공개 API)
    def _onboard_api(self, job: Job, source: dict, provider: Provider) -> str:
        """카탈로그에 있는 제공처: 키 확인 → 시험 호출 → 설정(method API) 활성화 → 첫 수집.

        키가 없으면 REVIEW/API_KEY_REQUIRED로 멈춘다 (관리자가 키를 넣으면 백엔드가 ONBOARD를 다시 넣는다).
        키가 틀리면 자격 증명을 INVALID로 표시하고 REVIEW/API_KEY_INVALID로 멈춘다.
        키워드 감시(source.kind WATCH)와 점검 소스(kind CHECK)는 백엔드가 만든 활성 설정을 그대로 쓰고 새 버전을 만들지 않는다.
        키워드 제공자(네이버 뉴스)의 도메인을 사이트처럼 등록하면 REVIEW/KEYWORDS_REQUIRED로 멈춘다.
        점검 소스의 첫 실행은 수집이 아니라 회사 상태 확인(_check)이다.
        """
        watch = source.get("kind") == "WATCH"
        check = source.get("kind") == "CHECK"
        self.db.update_site(source["site_id"], site_type="NEWS" if provider.keyword_based else "PUBLIC",
                            risk_flags={"api_provider": provider.id})
        if provider.keyword_based and not watch:
            raise CrawlFailure(Code.KEYWORDS_REQUIRED,
                               f"{provider.name.replace(' 검색', '')}는 키워드 감시로 등록합니다. "
                               "관리자 또는 등록 담당자가 키워드 묶음을 만들어 주세요.", stage="PROBE")
        self.db.update_registration(job.source_id, step="API_KEY", message=f"{provider.name} API 키를 확인하는 중입니다.")
        secret = self._load_secret(provider.id, provider, stage="PROBE")
        # 점검 소스는 제공자가 아니라 점검 종류(config.check)로 커넥터를 고른다 (data_go_kr 키 하나로 국세청·조달청)
        connector = (make_check_connector(source["config"], self.f) if check and source.get("config")
                     else make_connector(provider.id, self.f))
        try:
            connector.verify(secret)
        except CrawlFailure as e:
            e.stage = "PROBE"
            if e.code == Code.API_KEY_INVALID:
                self.db.mark_credential(provider.id, "INVALID", str(e))
            raise
        self.db.mark_credential(provider.id, "VALID", None)

        # 공시·공공 API는 6시간, 키워드 감시(보도 모니터링)는 60분, 회사 점검은 종류별 주기(백엔드가 정한 주기 우선)
        if check:
            interval = source.get("interval_minutes") or CHECK_INTERVALS.get(
                str((source.get("config") or {}).get("check") or ""), CHECK_INTERVAL_MINUTES)
        elif watch:
            # 백엔드가 정한 주기를 지킨다 (일반 감시 60분, 회사 감시 360분)
            interval = source.get("interval_minutes") or 60
        else:
            interval = defaults_for("PUBLIC")["interval_minutes"]
        if (watch or check) and source.get("config"):
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
        if check:
            stats = self._check(job, source, config, connector=connector, secret=secret)
        else:
            stats = self._crawl_api(job, source, config, connector=connector, secret=secret)
        self.db.mark_crawl_result(job.source_id, True, interval)
        status = "PARTIAL" if stats.errors else "SUCCESS"
        self.db.finish_job(job.id, status, stats.found, stats.new, stats.errors, config_id)
        what = "감시 등록 완료" if watch else "점검 등록 완료" if check else "등록 완료"
        if check and config.get("check") == "dart_corp_map":
            done = f"고유번호가 없는 회사 {stats.found}곳 중 {stats.new}곳의 DART 고유번호를 채웠고"
        elif check:
            done = f"회사 {stats.found}곳을 확인해 신호 {stats.new}건을 남겼고"
        else:
            done = f"{provider.name} API로 {stats.new}건을 받았고"
        self.db.update_registration(
            job.source_id, status="COMPLETED", step="DONE", message=f"{what}. {done} {interval}분마다 업데이트합니다.")
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
        if source.get("kind") == "CHECK":
            return self._check_job(job, source)
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
        interval, note = self._adapt_interval(job, source, stats)
        self.db.mark_crawl_result(job.source_id, True, interval)
        status = "PARTIAL" if stats.errors else "SUCCESS"
        self.db.finish_job(job.id, status, stats.found, stats.new, stats.errors,
                           source["active_config_version_id"], note=note)
        return status

    def _adapt_interval(self, job: Job, source: dict, stats: CrawlStats) -> tuple[int, str | None]:
        """주기 자동 조정 (SENSING_ADAPTIVE_INTERVAL). 사이트 소스(kind SITE)의 주기 수집에서만, 이번 실행을 포함한 최근
        adaptive_window회의 새 문서 평균으로 판단한다. 바뀌면 source.interval_minutes만 고치고 로그와 crawl_job.note에 남긴다.
        사람이 PUT으로 정한 주기(interval_locked)는 그대로 둔다. 키워드 감시는 백엔드가 정한 주기를 쓰므로 건드리지 않는다."""
        current = int(source["interval_minutes"])
        if not self.s.adaptive_interval or job.job_type != "CRAWL" or source.get("kind", "SITE") != "SITE":
            return current, None
        counts = [stats.new] + self.db.recent_new_counts(job.source_id, self.s.adaptive_window - 1)
        base = defaults_for(source.get("site_type") or "ETC")["interval_minutes"]
        new, note = next_interval(current, base, counts, window=self.s.adaptive_window, min_new=self.s.adaptive_min_new,
                                  max_articles=self.s.max_articles_per_run, max_interval=self.s.adaptive_max_interval,
                                  locked=bool(source.get("interval_locked")))
        if note:
            self.db.set_interval(job.source_id, new)
            log.info("adaptive interval source=%s domain=%s: %s", job.source_id, source.get("domain"), note)
        return new, note

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

        # 이 감시가 회사(거래처)의 감시면 걸린 기사를 그 회사에 연결한다 (NEWS 신호는 보강이 토픽을 붙인 뒤 enrich가 만든다)
        company_id = self.db.company_of_watch(job.source_id) if source.get("kind") == "WATCH" else None
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
                if company_id is not None:
                    self.db.add_document_company(doc_id, company_id, "WATCH")
            for att_url, name in item.attachments:
                self.db.add_attachment(doc_id, att_url, name)
        return stats

    # ------------------------------------------------------------------ CHECK (회사 단위 점검, M3)
    def _check_job(self, job: Job, source: dict) -> str:
        """점검 소스의 주기 실행: 모든 ACTIVE 회사를 점검 종류(config.check)대로 확인한다. 급감 감지는 적용하지 않는다."""
        config = source["config"]
        provider_id = config["provider"]
        secret = self._load_secret(provider_id, api_catalog.get(provider_id), stage="LIST")
        connector = make_check_connector(config, self.f)
        stats = self._check(job, source, config, connector=connector, secret=secret)
        if source["status"] in ("BLOCKED", "BROKEN"):   # 관리자 재점검 통과
            self.db.set_source_status(job.source_id, "ACTIVE")
        self.db.mark_crawl_result(job.source_id, True, source["interval_minutes"])
        status = "PARTIAL" if stats.errors else "SUCCESS"
        self.db.finish_job(job.id, status, stats.found, stats.new, stats.errors, source["active_config_version_id"])
        return status

    def _check(self, job: Job, source: dict, config: dict, *, connector, secret: str) -> CrawlStats:
        """점검 종류(config.check)별 회사 단위 확인. found는 대상 회사 수, new는 새로 남긴 신호(또는 채운 고유번호) 수.

        - nts_status: 국세청 사업자등록 상태 → BIZ_STATUS 신호 (company.last_biz_status 갱신)
        - dart_corp_map: corpCode.xml로 corp_code가 빈 회사의 DART 고유번호를 채운다 (신호 없음)
        - dart_disclosure: corp_code가 있는 회사의 최근 공시를 문서(document_company API)로 저장하고 보고서명 규칙으로 DISCLOSURE 신호
        - g2b_debarment: 조달청 부정당업자(입찰참가자격 제한)를 DEBARMENT HIGH 신호로
        """
        kind = str(config.get("check") or "nts_status")
        if kind == "nts_status":
            return self._check_nts_status(job, config, connector, secret)
        if kind == "dart_corp_map":
            return self._check_dart_corp_map(job, config, connector, secret)
        if kind == "dart_disclosure":
            return self._check_dart_disclosure(job, config, connector, secret)
        if kind == "g2b_debarment":
            return self._check_g2b_debarment(job, config, connector, secret)
        raise CrawlFailure(Code.UNEXPECTED, f"지원하지 않는 점검 종류: {kind}", stage="LIST")

    def _call_api(self, provider_id: str, fn: Callable[[], object]):
        """커넥터 호출. 키가 틀렸다는 응답이면 자격 증명을 INVALID로 표시하고 그대로 올린다."""
        try:
            return fn()
        except CrawlFailure as e:
            if e.code == Code.API_KEY_INVALID:
                self.db.mark_credential(provider_id, "INVALID", str(e))
            raise

    def _check_nts_status(self, job: Job, config: dict, connector, secret: str) -> CrawlStats:
        """국세청 사업자등록 상태 확인. 회사마다 마지막 상태를 company.last_biz_status에 두고, 계속사업자가 아니거나 이전 확인과
        달라졌으면 company_signal(BIZ_STATUS)을 남긴다. fingerprint(b_stt_cd + end_dt)가 같으면 다시 기록하지 않는다."""
        stats = CrawlStats()
        companies = self.db.companies_with_biz_no()
        if not companies:
            log.info("check source=%s: no company with biz_no", job.source_id)
            return stats
        statuses = self._call_api(config["provider"], lambda: connector.check(secret, [c["biz_no"] for c in companies]))
        by_no = {s.biz_no: s for s in statuses}
        now = datetime.now(timezone.utc)
        for c in companies:
            st = by_no.get(c["biz_no"])
            if st is None:
                continue   # 응답에 없는 번호: 다음 주기에 다시
            stats.found += 1
            signal = biz_status_signal(st, c["last_biz_status"])
            self.db.save_biz_status(c["id"], {**st.as_detail(), "checked_at": now.isoformat()})
            if signal is not None and self.db.add_company_signal(
                    c["id"], signal.kind, signal.severity, signal.fingerprint, signal.title, signal.detail, now,
                    source_id=job.source_id):
                stats.new += 1
                log.info("company %s (%s) signal %s/%s: %s", c["id"], c["name"], signal.kind, signal.severity, signal.title)
        return stats

    def _check_dart_corp_map(self, job: Job, config: dict, connector, secret: str) -> CrawlStats:
        """corp_code가 빈 ACTIVE 회사에 DART 고유번호를 채운다. found = 대상 회사 수, new = 새로 채운 수.
        이름(별칭 포함)이 정확히 일치하는 항목이 하나(또는 상장사가 하나)일 때만 채우고, 모호하면 로그만 남긴다."""
        stats = CrawlStats()
        companies = self.db.companies_without_corp_code()
        stats.found = len(companies)
        if not companies:
            log.info("check source=%s: every company already has a corp_code", job.source_id)
            return stats
        entries = self._call_api(config["provider"], lambda: connector.corp_codes(secret))
        index = build_corp_index(entries)
        for c in companies:
            pick = pick_corp_code(c["name"], c.get("aliases") or [], index)
            if pick.entry is not None:
                if self.db.set_corp_code(c["id"], pick.entry.corp_code):
                    stats.new += 1
                    log.info("company %s (%s) corp_code=%s (%s, matched on %s)", c["id"], c["name"], pick.entry.corp_code,
                             pick.entry.corp_name, pick.matched_on)
            elif pick.ambiguous:
                log.warning("company %s (%s): DART corp_code ambiguous on %s — %s", c["id"], c["name"], pick.matched_on,
                            ", ".join(f"{e.corp_code}({e.corp_name}{'·상장' if e.listed else ''})" for e in pick.candidates[:5]))
        log.info("dart corp map: companies=%s filled=%s entries=%s", stats.found, stats.new, len(entries))
        return stats

    def _check_dart_disclosure(self, job: Job, config: dict, connector, secret: str) -> CrawlStats:
        """corp_code가 있는 회사마다 최근 days일 공시를 받아 문서로 저장(document_company API)하고 DISCLOSURE 신호를 남긴다.
        found = 확인한 회사 수, new = 새 신호 수. 한 회사의 API 오류(키 오류 제외)는 기록하고 다음 회사로 넘어간다 (PARTIAL)."""
        stats = CrawlStats()
        companies = self.db.companies_with_corp_code()
        if not companies:
            log.info("check source=%s: no company with corp_code", job.source_id)
            return stats
        days = max(0, int(config.get("days", DISCLOSURE_DEFAULT_DAYS) or 0))
        now = datetime.now(timezone.utc)
        since = (now - timedelta(days=days)).date()
        docs_new = 0
        for c in companies:
            corp_config = {**config, "corp_codes": [c["corp_code"]]}
            try:
                items = self._call_api(config["provider"],
                                       lambda: connector.fetch(secret, corp_config, since, self.s.max_api_items_per_run))
            except CrawlFailure as e:
                if e.code in API_KEY_CODES:
                    raise
                self._record(job, e)
                stats.errors += 1
                log.warning("company %s (%s) disclosure fetch failed: %s %s", c["id"], c["name"], e.code.value, e)
                continue
            stats.found += 1
            for item in items:
                doc_id, result = self.db.upsert_document(
                    source_id=job.source_id, job_id=job.id, url=item.url, title=item.title, author=item.author,
                    section=item.section, published_at=item.published_at, body=item.body)
                if result == "NEW":
                    docs_new += 1
                elif result == "UPDATED":
                    stats.updated += 1
                self.db.add_document_company(doc_id, c["id"], "API")
                rcept_no = str(item.extra.get("rcept_no") or "") or sha256(item.url)
                signal = disclosure_signal(rcept_no, item.title, corp_name=item.author, rcept_dt=item.extra.get("rcept_dt"),
                                           url=item.url, flr_nm=item.section)
                if signal is not None and self.db.add_company_signal(
                        c["id"], signal.kind, signal.severity, signal.fingerprint, signal.title, signal.detail,
                        item.published_at or now, document_id=doc_id, source_id=job.source_id):
                    stats.new += 1
                    log.info("company %s (%s) signal %s/%s: %s", c["id"], c["name"], signal.kind, signal.severity, signal.title)
        log.info("dart disclosure: companies=%s new_documents=%s new_signals=%s errors=%s", stats.found, docs_new, stats.new,
                 stats.errors)
        return stats

    def _check_g2b_debarment(self, job: Job, config: dict, connector, secret: str) -> CrawlStats:
        """조달청 부정당업자 조회. 제재가 있는 회사마다 DEBARMENT HIGH 신호 (fingerprint 시작일+기관이라 같은 제재는 한 번만).
        found = 대조한 회사 수, new = 새 신호 수. observed_at은 제재 시작일(없으면 지금)."""
        stats = CrawlStats()
        companies = self.db.companies_for_debarment()
        stats.found = len(companies)
        if not companies:
            log.info("check source=%s: no active company", job.source_id)
            return stats
        result = self._call_api(config["provider"], lambda: connector.check(secret, companies, config))
        now = datetime.now(timezone.utc)
        names = {int(c["id"]): c["name"] for c in companies}
        for company_id, items in result.items():
            for d in items:
                signal = debarment_signal(d)
                observed = _kst_midnight(d.start_date) or now
                if self.db.add_company_signal(company_id, signal.kind, signal.severity, signal.fingerprint, signal.title,
                                              signal.detail, observed, source_id=job.source_id):
                    stats.new += 1
                    log.info("company %s (%s) signal %s/%s: %s", company_id, names.get(company_id), signal.kind,
                             signal.severity, signal.title)
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


def _kst_midnight(ymd: str | None) -> datetime | None:
    """"20260101" → 한국 시간 그날 자정 (제재 시작일을 observed_at으로 쓸 때). 형식이 아니면 None."""
    t = (ymd or "").strip()
    if len(t) != 8 or not t.isdigit():
        return None
    try:
        return datetime(int(t[:4]), int(t[4:6]), int(t[6:]), tzinfo=timezone(timedelta(hours=9)))
    except ValueError:
        return None


# ---------------------------------------------------------------------- 온보딩 판단 (DB 없이도 쓴다: evaluate.py)

@dataclass
class Probe:
    robots: str
    ai_blocked: bool
    site_type: str
    home: Page | None = None   # 사전 점검에서 받은 홈. 링크 모음 판정이 다시 받지 않고 쓴다


def probe_site(f: PoliteFetcher, domain: str, base_url: str) -> Probe:
    """사전 점검: robots·AI 봇 차단 여부, 홈 접속(차단·로그인 판정 포함), 사이트 유형."""
    robots = f.robots_status(base_url)
    ai_blocked = f.ai_bots_blocked(base_url)
    if robots == "DISALLOWED":
        raise CrawlFailure(Code.ROBOTS_DISALLOWED, url=base_url, stage="PROBE")
    home = f.get(base_url)
    return Probe(robots, ai_blocked, guess_site_type(domain, home.text), home)


def portal_batch_id(source_id: int) -> str:
    """모음 페이지 소스가 대신 등록한 언론사들의 묶음 id (registration_request.batch_id, VARCHAR(40))."""
    return f"portal:{source_id}"


def portal_message(registered: int, batch_id: str, duplicates: int = 0, over_cap: int = 0) -> str:
    """등록 요청 결과 메시지. 화면이 '묶음 portal:<id>'를 읽어 언론사들의 진행을 연다."""
    msg = MESSAGES[Code.PORTAL_EXPANDED].format(n=registered)
    notes = []
    if duplicates:
        notes.append(f"이미 등록된 {duplicates}곳 제외")
    if over_cap:
        notes.append(f"상한을 넘은 {over_cap}곳 제외")
    if notes:
        msg += f" ({', '.join(notes)})"
    return f"{msg} [묶음 {batch_id}]"


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

