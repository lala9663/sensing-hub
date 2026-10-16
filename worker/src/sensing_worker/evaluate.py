"""자동 탐색 성공률 측정: 실제 사이트 목록에 온보딩 판단(사전 점검 → 자동 탐색 → 품질 검사)만 돌려 본다.

DB에 쓰지 않고, 첫 수집도 하지 않는다. 요청 규칙(robots·간격·차단 시 중단)은 워커와 같다.

    sensing-discovery-eval docs/eval-sites.txt
    sensing-discovery-eval https://www.example.com https://news.example.org --json result.json

사이트 목록 파일은 한 줄에 "사이트주소 [샘플기사주소]", '#' 뒤는 메모로 무시한다. 샘플 기사 주소가 있으면 고른 패턴이
그 주소를 잡는지(sample_match)도 적는다 — 현업이 준 샘플 기사가 한 섹션에만 맞는 패턴에 빠지지 않는지 보는 용도.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .apis import lookup as api_lookup
from .codes import CrawlFailure
from .config import Settings
from .discovery import matches
from .fetcher import PoliteFetcher
from .jobs import discover, probe_site
from .llm import LlmClient, make_llm

# 온보딩 결과와 같은 분류: OK(자동 등록) | API(공개 API 소스, 키 필요) | REVIEW(관리자 큐) | BLOCKED(등록 불가) | ERROR(그 밖의 실패)
REVIEW_CODES = {"DISCOVERY_FAILED", "QUALITY_FAILED"}


@dataclass
class SiteResult:
    url: str
    outcome: str
    code: str | None = None
    message: str | None = None
    site_type: str | None = None
    method: str | None = None
    hubs: int = 0
    pattern: str | None = None
    quality: str | None = None
    requests: int = 0
    seconds: float = 0.0
    recipe_source: str = "heuristic"      # heuristic | llm (LLM 설정이 없으면 항상 heuristic)
    sample_url: str | None = None         # 목록 파일에 적은 샘플 기사 주소
    sample_match: bool | None = None      # 고른 패턴이 샘플 기사를 잡는가 (샘플이 없거나 실패하면 None)


def evaluate_site(url: str, settings: Settings, llm: LlmClient | None = None,
                  sample_url: str | None = None) -> SiteResult:
    url = url if "://" in url else "https://" + url
    parts = urlsplit(url)
    base_url = f"{parts.scheme}://{parts.netloc}"
    domain = (parts.hostname or "").lower().removeprefix("www.")
    provider = api_lookup(domain)
    if provider is not None:
        # 온보딩과 같이 크롤링하지 않는다. 키 발급은 사람의 일이므로 측정 도구는 네트워크를 쓰지 않는다
        return SiteResult(url=base_url, outcome="API", code="API_KEY_REQUIRED", method="API",
                          message=f"{provider.name}: API 키 발급 필요 ({provider.signup_url})")
    fetcher = PoliteFetcher(settings)
    llm = llm if llm is not None else make_llm(settings)
    started = time.monotonic()
    result = SiteResult(url=base_url, outcome="ERROR", sample_url=sample_url)
    try:
        probe = probe_site(fetcher, domain, base_url)
        result.site_type = probe.site_type
        found, result.quality = discover(fetcher, domain, base_url, probe.site_type, llm=llm, settings=settings)  # 온보딩과 같은 판단
        result.method, result.hubs = found.method, len(found.hubs)
        result.pattern = found.article_pattern.get("signature") or found.article_pattern.get("path")
        result.recipe_source = found.recipe_source
        if sample_url:
            result.sample_match = matches(sample_url, found.article_pattern)
        result.outcome = "OK"
    except CrawlFailure as e:
        result.code, result.message = e.code.value, str(e)
        if e.code.value in REVIEW_CODES:
            result.outcome = "REVIEW"
        elif e.blocking or e.code.value == "ROBOTS_DISALLOWED":
            result.outcome = "BLOCKED"
    except Exception as e:  # 측정 도구는 한 사이트 오류로 멈추지 않는다
        result.code, result.message = "UNEXPECTED", f"{type(e).__name__}: {e}"[:300]
    finally:
        result.requests = fetcher.request_count
        result.seconds = round(time.monotonic() - started, 1)
        fetcher.close()
    return result


def read_site_entries(args: list[str]) -> list[tuple[str, str | None]]:
    """(사이트 주소, 샘플 기사 주소 | None). 파일은 한 줄에 "사이트 [샘플]", 인자로 직접 준 주소는 샘플 없음."""
    entries: dict[str, str | None] = {}
    for arg in args:
        path = Path(arg)
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                fields = line.split("#", 1)[0].split()
                if fields:
                    entries.setdefault(fields[0], None)
                    if len(fields) > 1 and not entries[fields[0]]:
                        entries[fields[0]] = fields[1]
        else:
            entries.setdefault(arg, None)
    return list(entries.items())


def read_sites(args: list[str]) -> list[str]:
    return [site for site, _ in read_site_entries(args)]


def summarize(results: list[SiteResult], target: int | None = None) -> str:
    lines = [f"{'결과':<8}{'사이트':<34}{'방식':<9}{'레시피':<10}{'유형':<8}{'샘플':<6}{'요청':>5}{'초':>7}  비고"]
    for r in results:
        note = r.quality if r.outcome == "OK" else f"{r.code}: {r.message}"
        recipe = r.recipe_source if r.outcome == "OK" else "-"
        sample = "-" if r.sample_match is None else "O" if r.sample_match else "X"
        lines.append(f"{r.outcome:<8}{urlsplit(r.url).netloc[:33]:<34}{r.method or '-':<9}{recipe:<10}"
                     f"{r.site_type or '-':<8}{sample:<6}{r.requests:>5}{r.seconds:>7}  {note}")
    ok = sum(r.outcome == "OK" for r in results)
    goal = f" (목표 {target}곳: {'달성' if ok >= target else '미달'})" if target else ""
    lines.append(f"\n자동 등록 성공 {ok}/{len(results)}곳{goal}")
    counts = {k: sum(r.outcome == k for r in results) for k in ("API", "REVIEW", "BLOCKED", "ERROR")}
    lines.append("나머지: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    sampled = [r for r in results if r.sample_url]
    if sampled:
        hit = sum(bool(r.sample_match) for r in sampled)
        lines.append(f"샘플 기사 적중 {hit}/{len(sampled)}곳 (등록된 패턴이 현업 샘플 기사 주소를 잡는가)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="자동 탐색 성공률 측정 (DB 없이 실제 사이트에 온보딩 판단만 실행)")
    parser.add_argument("sites", nargs="+", help="사이트 주소 또는 주소 목록 파일")
    parser.add_argument("--json", help="사이트별 결과를 JSON으로 저장할 경로")
    parser.add_argument("--target", type=int, help="목표 성공 사이트 수 (미달이면 종료 코드 1)")
    parser.add_argument("--parallel", type=int, default=4, help="동시에 점검할 사이트 수 (도메인이 다르므로 간격 규칙과 무관)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    settings = Settings()
    llm = make_llm(settings)   # 설정이 없으면 NoLlm: 온보딩과 같이 휴리스틱만 측정한다
    sites = read_site_entries(args.sites)
    with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
        results = list(pool.map(lambda e: evaluate_site(e[0], settings, llm, sample_url=e[1]), sites))

    print(summarize(results, args.target))
    if args.json:
        Path(args.json).write_text(json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2),
                                   encoding="utf-8")
    ok = sum(r.outcome == "OK" for r in results)
    return 1 if args.target and ok < args.target else 0


if __name__ == "__main__":
    sys.exit(main())
