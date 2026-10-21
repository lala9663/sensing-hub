"""robots.txt 판정 (RFC 9309 Robots Exclusion Protocol).

urllib.robotparser는 그룹 안에서 위에서부터 처음 맞는 규칙을 쓰고 와일드카드(*)·끝 표시($)를 모른다. 서울신문처럼
"Allow: /"를 맨 앞에 두고 그 아래에 "Disallow: /xml/"을 적은 robots.txt에서는 Disallow가 모두 무시돼, 사이트가 막은
경로(/xml/rss/…)를 허용으로 판정했다. 여기서는 RFC 9309대로 판정한다.

- 그룹: User-agent 줄(이어진 여러 줄은 한 그룹) 뒤의 Allow·Disallow 규칙. 규칙 뒤에 오는 User-agent 줄이 새 그룹을 연다.
  첫 User-agent 앞의 규칙은 버린다. 빈 줄은 그룹을 끝내지 않는다.
- 그룹 고르기: 우리 제품 토큰(User-Agent 문자열의 "/" 앞, 대소문자 무시)과 같은 이름의 그룹들을 합쳐 쓴다. 없으면 "*" 그룹들.
  둘 다 없으면 모두 허용.
- 판정: 경로(+쿼리)에 맞는 규칙 중 패턴이 가장 긴 것(옥텟 수). 길이가 같으면 Allow. 맞는 규칙이 없으면 허용.
  값이 빈 규칙("Disallow:")은 아무것도 막지 않는다. /robots.txt 자체는 늘 허용.
- 패턴: "*"는 0글자 이상, 끝의 "$"는 경로 끝. 비교 전에 양쪽 퍼센트 인코딩을 같은 꼴로 맞춘다 (비 ASCII는 UTF-8 인코딩,
  예약되지 않은 글자의 %XX는 풀고 나머지 %XX는 대문자).
- 줄 형식: "#" 뒤는 주석, 키와 콜론 사이 공백 허용("Disallow : /xml/"), 키 대소문자 무시, 흔한 오타 몇 가지 허용.
- Sitemap 줄은 그룹과 관계없이 모두 모은다. Crawl-delay(비표준)는 그 그룹 값으로 읽어 둔다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

_KEYS = {
    "user-agent": "user-agent", "useragent": "user-agent", "user agent": "user-agent",
    "allow": "allow",
    "disallow": "disallow", "dissallow": "disallow", "dissalow": "disallow", "disalow": "disallow",
    "sitemap": "sitemap", "site-map": "sitemap",
    "crawl-delay": "crawl-delay", "crawldelay": "crawl-delay",
}
_PCT = re.compile(r"%([0-9A-Fa-f]{2})")
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


@dataclass
class Rule:
    allow: bool
    pattern: str                 # 퍼센트 인코딩을 맞춘 패턴 (길이 비교에 쓴다)
    regex: re.Pattern

    def matches(self, path: str) -> bool:
        return self.regex.match(path) is not None


@dataclass
class Group:
    agents: list[str] = field(default_factory=list)   # 소문자 제품 토큰 ("*" 포함)
    rules: list[Rule] = field(default_factory=list)
    crawl_delay: float | None = None


def normalize(text: str) -> str:
    """경로·패턴의 퍼센트 인코딩을 같은 꼴로: 비 ASCII·공백은 UTF-8 %XX로, 예약되지 않은 글자의 %XX는 글자로, 나머지 %XX는 대문자."""
    out = []
    for ch in text:
        if ord(ch) > 126 or ord(ch) <= 32:
            out.append("".join(f"%{b:02X}" for b in ch.encode("utf-8")))
        else:
            out.append(ch)

    def fix(m: re.Match) -> str:
        ch = chr(int(m.group(1), 16))
        return ch if ch in _UNRESERVED else "%" + m.group(1).upper()

    return _PCT.sub(fix, "".join(out))


def compile_pattern(pattern: str) -> re.Pattern:
    """robots 패턴 → 정규식 (앞에서부터 맞춘다). "*"는 아무 글자열, 끝의 "$"만 경로 끝이다."""
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    return re.compile(".*".join(re.escape(piece) for piece in body.split("*")) + ("$" if anchored else ""), re.S)


def product_token(user_agent: str) -> str:
    """User-Agent 문자열 또는 User-agent 줄 값의 제품 토큰: "SensingHubBot/0.1 (+contact…)" → "sensinghubbot"."""
    token = (user_agent or "").strip().split()[0] if (user_agent or "").strip() else ""
    return token.split("/", 1)[0].lower()


def target_path(url: str) -> str:
    """판정할 경로: 경로 + 쿼리 (조각 #… 은 뺀다). 빈 경로는 "/"."""
    parts = urlsplit(url)
    path = parts.path or "/"
    return normalize(path + ("?" + parts.query if parts.query else ""))


class RobotsRules:
    """robots.txt 한 파일의 판정기."""

    def __init__(self, text: str):
        self.groups: list[Group] = []
        self.sitemaps: list[str] = []
        self._parse(text or "")

    def _parse(self, text: str) -> None:
        current: Group | None = None
        in_agents = False          # 바로 앞 줄이 User-agent 줄인가 (이어진 User-agent 줄은 한 그룹)
        for raw in text.lstrip("﻿").splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = _KEYS.get(" ".join(key.split()).lower())
            value = value.strip()
            if key == "user-agent":
                if current is None or not in_agents:
                    current = Group()
                    self.groups.append(current)
                token = "*" if value.startswith("*") else product_token(value)
                if token:
                    current.agents.append(token)
                in_agents = True
            elif key in ("allow", "disallow"):
                in_agents = False
                if current is None or not value:
                    continue        # 그룹 밖 규칙은 버리고, 빈 값은 아무것도 막지 않는다
                if not value.startswith(("/", "*")):
                    value = "/" + value
                pattern = normalize(value)
                current.rules.append(Rule(key == "allow", pattern, compile_pattern(pattern)))
            elif key == "sitemap":
                if value and value not in self.sitemaps:
                    self.sitemaps.append(value)
            elif key == "crawl-delay":
                if current is not None:
                    try:
                        current.crawl_delay = float(value)
                    except ValueError:
                        pass

    def groups_for(self, user_agent: str) -> list[Group]:
        """이 에이전트에 적용되는 그룹들: 제품 토큰이 같은 그룹 전부, 없으면 "*" 그룹 전부."""
        token = product_token(user_agent)
        mine = [g for g in self.groups if token and token in g.agents]
        return mine or [g for g in self.groups if "*" in g.agents]

    def decide(self, user_agent: str, url: str) -> Rule | None:
        """판정에 쓴 규칙 (가장 긴 패턴, 같은 길이면 Allow). 맞는 규칙이 없으면 None(허용)."""
        path = target_path(url)
        best: Rule | None = None
        for group in self.groups_for(user_agent):
            for rule in group.rules:
                if not rule.matches(path):
                    continue
                if (best is None or len(rule.pattern) > len(best.pattern)
                        or (len(rule.pattern) == len(best.pattern) and rule.allow and not best.allow)):
                    best = rule
        return best

    def can_fetch(self, user_agent: str, url: str) -> bool:
        if urlsplit(url).path == "/robots.txt":
            return True
        rule = self.decide(user_agent, url)
        return rule is None or rule.allow

    def has_disallow(self, user_agent: str) -> bool:
        """이 에이전트에 적용되는 그룹에 (값이 있는) Disallow 규칙이 하나라도 있는가."""
        return any(not r.allow for g in self.groups_for(user_agent) for r in g.rules)

    def status(self, user_agent: str, root: str) -> str:
        """사이트 robots 상태 (site.robots_status): 홈이 금지면 DISALLOWED, 우리 그룹에 Disallow가 있으면 PARTIAL, 아니면 ALLOWED.
        (파일이 없으면 MISSING인데, 그때는 이 객체가 없다)"""
        if not self.can_fetch(user_agent, root.rstrip("/") + "/"):
            return "DISALLOWED"
        return "PARTIAL" if self.has_disallow(user_agent) else "ALLOWED"

    def crawl_delay(self, user_agent: str) -> float | None:
        delays = [g.crawl_delay for g in self.groups_for(user_agent) if g.crawl_delay is not None]
        return max(delays) if delays else None
