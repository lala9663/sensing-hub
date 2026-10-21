"""운영 점검: ACTIVE 사이트 소스마다 robots.txt를 새 판정(RFC 9309, robots.py)으로 다시 읽어 허브·기사 주소가 금지되는지 본다.

DB는 읽기만 한다 (연결을 read_only로 연다). 사이트(scheme://host)마다 robots.txt를 한 번만 받고, 도메인 간격은 워커와 같다
(PoliteFetcher). 예전 판정(urllib.robotparser: 그룹 안에서 처음 맞는 규칙)과 다르게 나온 주소를 따로 센다.

    PYTHONPATH=src python -m sensing_worker.robots_recheck --dsn postgresql://…/sensing --out result.md

- 허브: 설정의 켜진 기사 종류 허브 (article_patterns, 없으면 hubs). 공개 API 소스(method API)는 robots 대상이 아니라 뺀다.
- 기사 표본: 그 소스의 최근 문서 주소 --samples건.
- robots 상태: 워커와 같은 규칙 (MISSING | ALLOWED | PARTIAL | DISALLOWED). DB의 site.robots_status와 나란히 적는다.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.robotparser
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .codes import CrawlFailure
from .config import Settings
from .discovery import article_kinds
from .fetcher import PoliteFetcher
from .robots import RobotsRules

SOURCES_SQL = """
SELECT s.id, s.name, st.domain, st.base_url, st.robots_status, cv.version, cv.config
FROM source s
JOIN site st ON st.id = s.site_id
LEFT JOIN source_config_version cv ON cv.id = s.active_config_version_id
WHERE s.kind = 'SITE' AND s.status = 'ACTIVE'
ORDER BY s.id
"""
SAMPLES_SQL = "SELECT url FROM document WHERE source_id = %s ORDER BY collected_at DESC, id DESC LIMIT %s"


@dataclass
class Robots:
    root: str
    status: str                      # MISSING | ALLOWED | PARTIAL | DISALLOWED
    http: str                        # robots.txt 응답 (200, 404, 오류 코드)
    rules: RobotsRules | None = None
    old: urllib.robotparser.RobotFileParser | None = None


@dataclass
class UrlCheck:
    url: str
    allowed: bool
    old_allowed: bool
    rule: str | None                 # 판정에 쓴 규칙 ("Disallow: /xml/"), 맞는 규칙이 없으면 None


@dataclass
class SourceCheck:
    id: int
    name: str
    domain: str
    method: str
    config_version: int | None
    db_status: str | None
    status: str = "MISSING"
    http: str = ""
    crawl_delay: float | None = None
    hubs: list[UrlCheck] = field(default_factory=list)
    articles: list[UrlCheck] = field(default_factory=list)
    note: str = ""

    @property
    def blocked_hubs(self) -> list[UrlCheck]:
        return [c for c in self.hubs if not c.allowed]

    @property
    def blocked_articles(self) -> list[UrlCheck]:
        return [c for c in self.articles if not c.allowed]

    @property
    def changed(self) -> list[UrlCheck]:
        return [c for c in self.hubs + self.articles if c.allowed != c.old_allowed]


def root_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def site_key(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


class RobotsCache:
    """사이트(scheme://host)마다 robots.txt를 한 번만 받는다. 판정 규칙은 PoliteFetcher._load_robots와 같다."""

    def __init__(self, fetcher: PoliteFetcher, user_agent: str):
        self.f = fetcher
        self.ua = user_agent
        self._cache: dict[str, Robots] = {}

    def get(self, url: str) -> Robots:
        root = root_of(url)
        if root not in self._cache:
            self._cache[root] = self._load(root)
        return self._cache[root]

    def _load(self, root: str) -> Robots:
        try:
            page = self.f.get_api(root + "/robots.txt")
        except CrawlFailure as e:
            return Robots(root, "MISSING", e.code.value)
        body = page.text if page.status == 200 else ""
        if page.status != 200 or "<html" in body[:500].lower():
            return Robots(root, "MISSING", str(page.status) + (" (HTML)" if page.status == 200 else ""))
        rules = RobotsRules(body)
        old = urllib.robotparser.RobotFileParser()
        old.parse(body.splitlines())
        return Robots(root, rules.status(self.ua, root), "200", rules, old)

    def check(self, url: str) -> UrlCheck:
        r = self.get(url)
        if r.rules is None:
            return UrlCheck(url, True, True, None)
        rule = r.rules.decide(self.ua, url)
        text = f"{'Allow' if rule.allow else 'Disallow'}: {rule.pattern}" if rule else None
        return UrlCheck(url, r.rules.can_fetch(self.ua, url), r.old.can_fetch(self.ua, url), text)


def hubs_of(config: dict) -> list[str]:
    try:
        kinds = article_kinds(config)
    except (KeyError, TypeError):
        kinds = []
    return list(dict.fromkeys(h for k in kinds for h in k.get("hubs") or [])) or list(config.get("hubs") or [])


def recheck(conn, fetcher: PoliteFetcher, user_agent: str, samples: int = 5,
            only: list[int] | None = None) -> tuple[list[SourceCheck], int]:
    """(소스별 결과, robots.txt를 받은 호스트 수)"""
    cache = RobotsCache(fetcher, user_agent)
    out: list[SourceCheck] = []
    rows = conn.execute(SOURCES_SQL).fetchall()
    for sid, name, domain, base_url, db_status, version, config in rows:
        if only and sid not in only:
            continue
        config = config or {}
        method = config.get("method") or "-"
        check = SourceCheck(sid, name or domain, domain, method, version, db_status)
        out.append(check)
        if method == "API":
            check.note = "공개 API 소스 (robots 대상 아님)"
            continue
        hubs = hubs_of(config)
        urls = [u for (u,) in conn.execute(SAMPLES_SQL, (sid, samples)).fetchall()]
        # 사이트 robots.txt는 한 번만: 등록 주소(http://domain)가 실제 수집 호스트(https://www.domain)로 넘어가는 사이트가
        # 많으므로, 같은 사이트의 허브·기사 호스트가 있으면 그 robots.txt로 사이트 상태를 본다
        same = [root_of(u) for u in hubs + urls if site_key(u) == site_key(base_url)]
        main = same[0] if same else root_of(base_url)
        home = cache.get(main)
        check.status, check.http = home.status, home.http
        check.crawl_delay = home.rules.crawl_delay(user_agent) if home.rules else None
        check.hubs = [cache.check(h) for h in hubs]
        check.articles = [cache.check(u) for u in urls]
        other = sorted({root_of(c.url) for c in check.hubs + check.articles} - {main})
        if other:
            check.note = "다른 호스트 robots도 확인: " + ", ".join(urlsplit(r).netloc for r in other)
    return out, len(cache._cache)


def _cell(checks: list[UrlCheck]) -> str:
    if not checks:
        return "-"
    blocked = [c for c in checks if not c.allowed]
    if not blocked:
        return f"0/{len(checks)}"
    rules = sorted({c.rule for c in blocked if c.rule})
    return f"**{len(blocked)}/{len(checks)}** ({', '.join(f'`{r}`' for r in rules)})"


def render(results: list[SourceCheck], requests: int, hosts: int, checked_at: str) -> str:
    lines = [f"점검 시각 {checked_at}, ACTIVE 사이트 소스 {len(results)}개, robots.txt {hosts}곳(호스트) 요청 {requests}회"
             f" (리다이렉트 포함).", "",
             "| 소스 | 사이트 | 방식 | 설정 | robots (DB → 새 판정) | Crawl-delay | 허브 금지 | 기사 표본 금지 | 예전 판정과 다름 | 비고 |",
             "|---:|---|---|---:|---|---:|---|---|---:|---|"]
    for r in results:
        robots = f"{r.db_status or '-'} → {r.status}" + (f" ({r.http})" if r.status == "MISSING" and r.http else "")
        if r.method == "API":
            robots = "-"
        delay = "-" if r.crawl_delay is None else f"{r.crawl_delay:g}"
        changed = len(r.changed)
        lines.append(f"| {r.id} | {r.domain} | {r.method} | v{r.config_version or '-'} | {robots} | {delay} | "
                     f"{_cell(r.hubs)} | {_cell(r.articles)} | {f'**{changed}**' if changed else 0} | {r.note} |")
    return "\n".join(lines)


def render_blocked(results: list[SourceCheck]) -> str:
    hit = [r for r in results if r.blocked_hubs or r.blocked_articles or r.status == "DISALLOWED"]
    if not hit:
        return "새 판정으로 금지되는 허브·기사 주소가 없습니다."
    lines = []
    for r in hit:
        lines.append(f"- 소스 {r.id} {r.domain} ({r.method}, 설정 v{r.config_version}, robots {r.status})")
        for c in r.blocked_hubs:
            lines.append(f"  - 허브 금지: {c.url} ← `{c.rule}`" + ("" if c.old_allowed else " (예전 판정도 금지)"))
        for c in r.blocked_articles:
            lines.append(f"  - 기사 금지: {c.url} ← `{c.rule}`" + ("" if c.old_allowed else " (예전 판정도 금지)"))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ACTIVE 사이트 소스의 robots.txt를 새 판정으로 다시 점검 (DB 읽기 전용)")
    parser.add_argument("--dsn", default=Settings().db_url, help="PostgreSQL 주소 (읽기만 한다)")
    parser.add_argument("--samples", type=int, default=5, help="소스마다 볼 최근 기사 주소 수")
    parser.add_argument("--source", type=int, action="append", help="이 소스만 (여러 번 줄 수 있다)")
    parser.add_argument("--out", help="마크다운 표를 저장할 경로")
    parser.add_argument("--json", help="소스별 결과를 JSON으로 저장할 경로")
    args = parser.parse_args(argv)

    import psycopg

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    settings = Settings()
    fetcher = PoliteFetcher(settings)
    checked_at = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %z")
    try:
        with psycopg.connect(args.dsn, connect_timeout=10) as conn:
            conn.read_only = True
            results, hosts = recheck(conn, fetcher, settings.user_agent, args.samples, args.source)
    finally:
        fetcher.close()
    text = render(results, fetcher.request_count, hosts, checked_at) + "\n\n" + render_blocked(results) + "\n"
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    if args.json:
        Path(args.json).write_text(json.dumps([{**asdict(r), "blocked_hubs": len(r.blocked_hubs),
                                                "blocked_articles": len(r.blocked_articles)} for r in results],
                                              ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
