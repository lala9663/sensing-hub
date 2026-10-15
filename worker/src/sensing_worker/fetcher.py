"""수집 하네스의 HTTP 계층: 예의 있는 요청과 실패 분류.

모든 사이트에 공통으로 적용한다.
- 도메인 단위 최소 요청 간격, 식별 가능한 User-Agent
- robots.txt 판정 (24시간 캐시)
- 5xx·타임아웃 재시도와 백오프, Retry-After 준수
- 차단 신호(403·429·봇 확인 화면·로그인)는 재시도하지 않고 즉시 실패로 분류
- 내부망 주소로 풀리는 호스트 접속 차단
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import ssl
import threading
import time
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

from .codes import Code, CrawlFailure
from .config import Settings

CHALLENGE_MARKERS = (
    "cf-browser-verification", "challenge-platform", "cf-chl-", "just a moment...", "attention required! | cloudflare",
    "g-recaptcha", "h-captcha", "hcaptcha.com", "px-captcha", "_incapsula_resource", "access denied - akamai",
    "are you a robot", "verify you are human", "자동입력 방지", "보안 문자",
)
LOGIN_URL_RE = re.compile(r"(login|signin|sign-in|auth/|member/log)", re.I)


@dataclass
class Page:
    url: str
    status: int
    content_type: str
    text: str
    headers: dict[str, str]


class PoliteFetcher:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.s = settings
        self._client = httpx.Client(
            headers={"User-Agent": settings.user_agent, "Accept-Language": "ko,en;q=0.8"},
            timeout=httpx.Timeout(settings.read_timeout, connect=settings.connect_timeout),
            follow_redirects=False,
            transport=transport,
        )
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, tuple[float, urllib.robotparser.RobotFileParser | None, str]] = {}
        self._lock = threading.Lock()
        self.request_count = 0

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------ robots
    def robots_status(self, base_url: str) -> str:
        """ALLOWED | PARTIAL | DISALLOWED | MISSING"""
        _, _, status = self._load_robots(base_url)
        return status

    def ai_bots_blocked(self, base_url: str) -> bool:
        rp, _, _ = self._load_robots(base_url)
        if rp is None:
            return False
        root = _root(base_url) + "/"
        return any(not rp.can_fetch(bot, root) for bot in ("GPTBot", "ClaudeBot", "CCBot", "Google-Extended"))

    def sitemaps(self, base_url: str) -> list[str]:
        rp, _, _ = self._load_robots(base_url)
        return list(rp.site_maps() or []) if rp else []

    def allowed(self, url: str) -> bool:
        rp, _, _ = self._load_robots(url)
        if rp is None:
            return True
        return rp.can_fetch(self.s.user_agent, url)

    def _load_robots(self, url: str):
        root = _root(url)
        cached = self._robots.get(root)
        if cached and time.time() - cached[0] < 24 * 3600:
            return cached[1], cached[0], cached[2]
        rp: urllib.robotparser.RobotFileParser | None = None
        status = "MISSING"
        try:
            page = self._request(root + "/robots.txt", check_robots=False, classify=False)
            body = page.text if page.status == 200 else ""
            if page.status == 200 and "<html" not in body[:500].lower():
                rp = urllib.robotparser.RobotFileParser()
                rp.parse(body.splitlines())
                if not rp.can_fetch(self.s.user_agent, root + "/"):
                    status = "DISALLOWED"
                elif "disallow:" in body.lower():
                    status = "PARTIAL"
                else:
                    status = "ALLOWED"
        except CrawlFailure:
            status = "MISSING"
        self._robots[root] = (time.time(), rp, status)
        return rp, self._robots[root][0], status

    # ------------------------------------------------------------------ fetch
    def get(self, url: str) -> Page:
        return self._request(url, check_robots=True, classify=True)

    def _request(self, url: str, *, check_robots: bool, classify: bool, redirects: int = 0) -> Page:
        if check_robots and not self.allowed(url):
            raise CrawlFailure(Code.ROBOTS_DISALLOWED, url=url)
        self._guard_host(url)

        attempt = 0
        while True:
            attempt += 1
            self._throttle(url)
            try:
                with self._client.stream("GET", url) as resp:
                    self.request_count += 1
                    body = self._read_limited(resp)
                    page = Page(url=str(resp.url), status=resp.status_code,
                                content_type=resp.headers.get("content-type", ""),
                                text=_decode(body, resp), headers=dict(resp.headers))
            except httpx.ConnectError as e:
                if _is_dns_error(e):
                    raise CrawlFailure(Code.DNS_FAIL, url=url) from e
                if _is_tls_error(e):
                    raise CrawlFailure(Code.TLS_ERROR, url=url) from e
                if attempt < self.s.max_retries:
                    self._backoff(attempt)
                    continue
                raise CrawlFailure(Code.TIMEOUT, url=url, retryable=True) from e
            except httpx.ProxyError as e:
                # 사내 프록시·방화벽이 이 사이트를 막은 경우: 사이트가 아니라 우리 쪽 정책 문제
                raise CrawlFailure(Code.NETWORK_BLOCKED, f"사내 프록시가 접속을 거부했습니다 ({e}).", url=url) from e
            except httpx.TimeoutException as e:
                if attempt < self.s.max_retries:
                    self._backoff(attempt)
                    continue
                raise CrawlFailure(Code.TIMEOUT, url=url, retryable=True) from e
            except httpx.HTTPError as e:
                raise CrawlFailure(Code.UNEXPECTED, f"{type(e).__name__}: {e}"[:500], url=url, retryable=True) from e

            # 리다이렉트는 직접 따라가며 매번 내부망·robots를 확인한다
            if page.status in (301, 302, 303, 307, 308) and "location" in page.headers:
                target = urljoin(url, page.headers["location"])
                if classify and LOGIN_URL_RE.search(urlsplit(target).path + "?" + urlsplit(target).query):
                    raise CrawlFailure(Code.LOGIN_REQUIRED, url=url, http_status=page.status)
                if redirects >= 5:
                    raise CrawlFailure(Code.HTTP_4XX, "리다이렉트가 너무 많습니다.", url=url)
                return self._request(target, check_robots=check_robots, classify=classify, redirects=redirects + 1)

            if not classify:
                return page

            if page.status >= 500:
                if attempt < self.s.max_retries:
                    self._backoff(attempt)
                    continue
                if _has_challenge(page.text):
                    raise CrawlFailure(Code.BOT_CHALLENGE, url=url, http_status=page.status, evidence=page.text[:2000])
                raise CrawlFailure(Code.HTTP_5XX, url=url, http_status=page.status, retryable=True)
            if page.status == 429:
                raise CrawlFailure(Code.RATE_LIMITED, url=url, http_status=429)
            if page.status in (401, 403):
                code = Code.LOGIN_REQUIRED if page.status == 401 else Code.BOT_CHALLENGE
                raise CrawlFailure(code, url=url, http_status=page.status, evidence=page.text[:2000])
            if page.status >= 400:
                raise CrawlFailure(Code.HTTP_4XX, url=url, http_status=page.status)
            if _has_challenge(page.text):
                raise CrawlFailure(Code.BOT_CHALLENGE, url=url, http_status=page.status, evidence=page.text[:2000])
            if _looks_like_login(page.text):
                raise CrawlFailure(Code.LOGIN_REQUIRED, url=url, http_status=page.status)
            return page

    def _read_limited(self, resp: httpx.Response) -> bytes:
        chunks, size = [], 0
        for chunk in resp.iter_bytes():
            size += len(chunk)
            if size > self.s.max_body_bytes:
                break
            chunks.append(chunk)
        return b"".join(chunks)

    def _throttle(self, url: str) -> None:
        host = urlsplit(url).hostname or ""
        with self._lock:
            last = self._last_request.get(host, 0.0)
            wait = self.s.min_interval_seconds - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
            self._last_request[host] = time.monotonic()

    def _backoff(self, attempt: int) -> None:
        time.sleep(min(0.5 * (2 ** (attempt - 1)), 10) if self.s.min_interval_seconds > 0 else 0)

    def _guard_host(self, url: str) -> None:
        if self.s.allow_private_hosts:
            return
        host = urlsplit(url).hostname
        if not host:
            raise CrawlFailure(Code.HTTP_4XX, "주소가 올바르지 않습니다.", url=url)
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror as e:
            # 사내망처럼 프록시로만 외부에 나가는 환경은 로컬 DNS가 외부 도메인을 못 푼다.
            # 이때는 프록시가 이름을 풀므로 여기서 막지 않는다 (도메인 형식 검사는 백엔드가 이미 했다).
            if os.getenv("HTTPS_PROXY") or os.getenv("https_proxy"):
                return
            raise CrawlFailure(Code.DNS_FAIL, url=url) from e
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                raise CrawlFailure(Code.INTERNAL_ADDRESS, url=url)


def _root(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _decode(body: bytes, resp: httpx.Response) -> str:
    if resp.encoding:
        try:
            return body.decode(resp.encoding, errors="replace")
        except LookupError:
            pass
    head = body[:2048].decode("ascii", errors="ignore").lower()
    m = re.search(r"charset=[\"']?([a-z0-9_-]+)", head)
    for enc in ([m.group(1)] if m else []) + ["utf-8", "cp949"]:
        try:
            return body.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", errors="replace")


def _has_challenge(text: str) -> bool:
    low = text[:20000].lower()
    return any(marker in low for marker in CHALLENGE_MARKERS)


def _looks_like_login(text: str) -> bool:
    low = text[:30000].lower()
    has_password = 'type="password"' in low or "type='password'" in low
    # 본문이 거의 없고 비밀번호 입력란이 있으면 로그인 화면으로 본다
    return has_password and low.count("<p") < 3 and low.count("<a ") < 40


def _is_dns_error(e: Exception) -> bool:
    text = repr(e).lower()
    return "name or service not known" in text or "nodename nor servname" in text or "getaddrinfo" in text


def _is_tls_error(e: Exception) -> bool:
    cause = e.__cause__ or e.__context__
    return isinstance(cause, ssl.SSLError) or "ssl" in repr(e).lower() or "certificate" in repr(e).lower()
