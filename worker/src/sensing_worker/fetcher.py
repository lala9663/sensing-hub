"""수집 하네스의 HTTP 계층: 예의 있는 요청과 실패 분류.

모든 사이트에 공통으로 적용한다.
- 도메인 단위 최소 요청 간격, 식별 가능한 User-Agent
- robots.txt 판정 (24시간 캐시)
- 5xx·타임아웃 재시도와 백오프, Retry-After 준수
- 차단 신호(403·429·봇 확인 화면·로그인)는 재시도하지 않고 즉시 실패로 분류
- 내부망 주소로 풀리는 호스트 접속 차단
- 구식 TLS 설정을 요구하는 서버(공공기관에 흔함)는 인증서 검증을 유지한 호환 컨텍스트로 한 번 더 시도
"""

from __future__ import annotations

import codecs
import ipaddress
import os
import re
import socket
import ssl
import time
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

from .codes import Code, CrawlFailure
from .config import Settings
from .throttle import DbThrottle, LocalThrottle

CHALLENGE_MARKERS = (
    "cf-browser-verification", "challenge-platform", "cf-chl-", "just a moment...", "attention required! | cloudflare",
    "g-recaptcha", "h-captcha", "hcaptcha.com", "px-captcha", "_incapsula_resource", "access denied - akamai",
    "are you a robot", "verify you are human", "자동입력 방지", "보안 문자",
)
LOGIN_URL_RE = re.compile(r"(login|signin|sign-in|auth/|member/log)", re.I)
# 본문이 거의 없는 "넘김" 페이지: <meta refresh> 또는 location.href = "..." 한 줄짜리 홈 (공공기관에 흔하다)
CLIENT_REDIRECT_MAX_BYTES = 4096
META_REFRESH_RE = re.compile(r"""<meta[^>]+http-equiv=["']?refresh["']?[^>]+content=["']?\s*\d+\s*;\s*url=([^"'>\s]+)""", re.I)
JS_REDIRECT_RE = re.compile(r"""(?:location|window\.location|document\.location)(?:\.href)?\s*=\s*["']([^"']+)["']"""
                            r"""|location\.replace\(\s*["']([^"']+)["']\s*\)""", re.I)
# 실패 증거로 남길 응답 본문 길이 (문자)
EVIDENCE_CHARS = 20000
# 바이너리 요청(get_api_bytes)에서 텍스트로도 풀어 두는 최대 크기: 오류 응답(JSON·XML)은 이보다 작고 ZIP은 이보다 크다
BINARY_TEXT_PEEK_BYTES = 65536
# OpenSSL SSL_OP_LEGACY_SERVER_CONNECT. Python 3.12부터 ssl 모듈에 상수가 있다
_OP_LEGACY_SERVER_CONNECT = getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)


@dataclass
class Page:
    url: str
    status: int
    content_type: str
    text: str
    headers: dict[str, str]
    # get_api_bytes로 받은 원본 바이트 (ZIP 등 바이너리 응답). 텍스트 요청에서는 비어 있다
    body: bytes = b""
    # 응답이 크기 상한(max_body_bytes 또는 호출자가 준 max_bytes)을 넘어 잘렸는지
    truncated: bool = False


def compat_ssl_context() -> ssl.SSLContext:
    """구식 TLS 서버용 호환 컨텍스트. 인증서 검증(체인·호스트명)은 기본 컨텍스트 그대로 유지한다.

    공공기관 사이트 중에는 RSA 키 교환 암호만 지원하거나(ECDHE 없음 → Python 기본 암호 목록에서 제외),
    1024비트 DH·SHA-1 서명처럼 OpenSSL SECLEVEL 2가 거부하는 설정이거나, legacy renegotiation을
    요구하는 곳이 있다. 세 가지를 모두 허용하되 TLS 1.2 미만은 쓰지 않는다.
    """
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
    ctx.options |= _OP_LEGACY_SERVER_CONNECT
    return ctx


class PoliteFetcher:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None,
                 throttle: LocalThrottle | DbThrottle | None = None,
                 compat_transport: httpx.BaseTransport | None = None):
        self.s = settings
        self._client = self._make_client(transport)
        # 호환 TLS 클라이언트는 처음 필요할 때 만든다. 테스트는 compat_transport로 두 클라이언트를 구분한다
        self._compat_transport = compat_transport or transport
        self._compat_client: httpx.Client | None = None
        # 호환 컨텍스트로만 접속되는 호스트. 한 번 알아내면 fetcher 수명(워커 프로세스) 동안 바로 호환 클라이언트로 보낸다
        self.compat_hosts: set[str] = set()
        # 워커 여러 대가 돌 때는 DbThrottle을 넘겨 도메인 간격을 공유한다
        self.throttle = throttle or LocalThrottle(settings.min_interval_seconds)
        self._robots: dict[str, tuple[float, urllib.robotparser.RobotFileParser | None, str]] = {}
        self.request_count = 0

    def _make_client(self, transport: httpx.BaseTransport | None, verify: ssl.SSLContext | bool = True) -> httpx.Client:
        return httpx.Client(
            headers={"User-Agent": self.s.user_agent, "Accept-Language": "ko,en;q=0.8"},
            timeout=httpx.Timeout(self.s.read_timeout, connect=self.s.connect_timeout),
            follow_redirects=False,
            transport=transport,
            verify=verify,
        )

    def _compat(self) -> httpx.Client:
        if self._compat_client is None:
            self._compat_client = self._make_client(self._compat_transport, verify=compat_ssl_context())
        return self._compat_client

    def close(self) -> None:
        self._client.close()
        if self._compat_client is not None:
            self._compat_client.close()
        self.throttle.close()

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

    def get_api(self, url: str, headers: dict[str, str] | None = None) -> Page:
        """공개 API 호출. 도메인 간격·내부망 차단·연결 재시도는 같지만 robots 검사와 차단 화면 분류는 하지 않는다
        (API 주소는 robots 대상이 아니고, 오류는 응답 JSON의 상태 코드로 커넥터가 분류한다). 4xx·5xx도 그대로 돌려준다.
        headers: 헤더로 인증하는 API(네이버 X-Naver-Client-Id 등)용. 로그에 남기지 않는다."""
        return self._request(url, check_robots=False, classify=False, headers=headers)

    def post_api(self, url: str, json_body, headers: dict[str, str] | None = None) -> Page:
        """JSON 본문을 POST하는 공개 API 호출 (국세청 사업자 상태조회 등). 그 밖은 get_api와 같다.
        리다이렉트는 따라가지 않는다 (POST 본문을 다른 호스트로 다시 보내지 않기 위해)."""
        return self._request(url, check_robots=False, classify=False, headers=headers, method="POST", json_body=json_body)

    def get_api_bytes(self, url: str, headers: dict[str, str] | None = None, max_bytes: int | None = None) -> Page:
        """바이너리 응답(DART corpCode.xml ZIP 등)을 받는 공개 API 호출. 원본은 Page.body에, 텍스트 디코딩은
        작은 응답(오류 JSON·XML, 64KB 이하)에만 한다. max_bytes를 넘으면 Page.truncated가 True다 (커넥터가 실패로 분류)."""
        return self._request(url, check_robots=False, classify=False, headers=headers, binary=True, max_bytes=max_bytes)

    def _request(self, url: str, *, check_robots: bool, classify: bool, redirects: int = 0,
                 headers: dict[str, str] | None = None, method: str = "GET", json_body=None,
                 binary: bool = False, max_bytes: int | None = None) -> Page:
        if check_robots and not self.allowed(url):
            raise CrawlFailure(Code.ROBOTS_DISALLOWED, url=url)
        self._guard_host(url)

        host = urlsplit(url).hostname or ""
        use_compat = host in self.compat_hosts
        attempt = 0
        while True:
            attempt += 1
            self._throttle(url)
            client = self._compat() if use_compat else self._client
            try:
                with client.stream(method, url, headers=headers, json=json_body) as resp:
                    self.request_count += 1
                    if use_compat:
                        self.compat_hosts.add(host)
                    body, truncated = self._read_limited(resp, max_bytes or self.s.max_body_bytes)
                    text = _decode(body, resp) if (not binary or len(body) <= BINARY_TEXT_PEEK_BYTES) else ""
                    page = Page(url=str(resp.url), status=resp.status_code,
                                content_type=resp.headers.get("content-type", ""),
                                text=text, headers=dict(resp.headers), body=body if binary else b"", truncated=truncated)
            except httpx.ConnectError as e:
                if _is_dns_error(e):
                    raise CrawlFailure(Code.DNS_FAIL, url=url) from e
                if _is_tls_error(e):
                    if _is_cert_error(e):
                        # 서버 인증서 자체가 문제(만료·체인·호스트명 불일치). 호환 설정으로 넘어가지 않는다
                        raise CrawlFailure(Code.TLS_ERROR, f"인증서 검증 실패: {_ssl_detail(e)}", url=url) from e
                    if self.s.tls_compat and not use_compat:
                        # 구식 TLS 설정을 요구하는 서버일 수 있다: 호환 컨텍스트로 한 번만 다시 시도 (재시도 횟수에 안 셈)
                        use_compat = True
                        attempt -= 1
                        continue
                    detail = (f"구식 TLS 호환 설정(SECLEVEL=1, TLS 1.2+)으로도 실패: {_ssl_detail(e)}" if use_compat
                              else f"TLS 핸드셰이크 실패 (호환 재시도 꺼짐): {_ssl_detail(e)}")
                    raise CrawlFailure(Code.TLS_ERROR, f"보안 연결(TLS)에 실패했습니다. {detail}", url=url) from e
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

            if method == "POST":
                return page
            # 리다이렉트는 직접 따라가며 매번 내부망·robots를 확인한다
            if page.status in (301, 302, 303, 307, 308) and "location" in page.headers:
                target = urljoin(url, page.headers["location"])
                if classify and LOGIN_URL_RE.search(urlsplit(target).path + "?" + urlsplit(target).query):
                    raise CrawlFailure(Code.LOGIN_REQUIRED, url=url, http_status=page.status)
                if redirects >= 5:
                    raise CrawlFailure(Code.HTTP_4XX, "리다이렉트가 너무 많습니다.", url=url)
                return self._request(target, check_robots=check_robots, classify=classify, redirects=redirects + 1,
                                     headers=headers if same_host(url, target) else None, binary=binary, max_bytes=max_bytes)

            if not classify:
                return page

            # 홈이 스크립트 한 줄로 다른 주소로 넘기는 경우도 리다이렉트처럼 따라간다
            target = client_redirect_target(page)
            if target and redirects < 5:
                return self._request(urljoin(page.url, target), check_robots=check_robots, classify=classify,
                                     redirects=redirects + 1)

            if page.status >= 500:
                if attempt < self.s.max_retries:
                    self._backoff(attempt)
                    continue
                evidence = page.text[:EVIDENCE_CHARS]
                if _has_challenge(page.text):
                    raise CrawlFailure(Code.BOT_CHALLENGE, url=url, http_status=page.status, evidence=evidence)
                raise CrawlFailure(Code.HTTP_5XX, url=url, http_status=page.status, retryable=True, evidence=evidence)
            evidence = page.text[:EVIDENCE_CHARS]
            if page.status == 429:
                raise CrawlFailure(Code.RATE_LIMITED, url=url, http_status=429, evidence=evidence)
            if page.status == 401:
                raise CrawlFailure(Code.LOGIN_REQUIRED, url=url, http_status=401, evidence=evidence)
            if page.status == 403:
                # 봇 확인 화면이 보이면 차단(BLOCKED). 그렇지 않은 403은 "이 경로는 거부"일 뿐이다 — 주간조선처럼
                # 탐색기가 넘겨본 /rss/ 하나가 403이라고 사이트 전체를 차단으로 끝내면 안 된다. 홈이 403이면
                # probe_site가 사이트 단위 차단으로 올린다
                if _has_challenge(page.text):
                    raise CrawlFailure(Code.BOT_CHALLENGE, url=url, http_status=403, evidence=evidence)
                raise CrawlFailure(Code.HTTP_4XX, "접근이 거부된 페이지입니다 (403).", url=url, http_status=403,
                                   evidence=evidence)
            if page.status >= 400:
                raise CrawlFailure(Code.HTTP_4XX, url=url, http_status=page.status, evidence=evidence)
            if _has_challenge(page.text):
                raise CrawlFailure(Code.BOT_CHALLENGE, url=url, http_status=page.status, evidence=evidence)
            if _looks_like_login(page.text):
                raise CrawlFailure(Code.LOGIN_REQUIRED, url=url, http_status=page.status, evidence=evidence)
            return page

    def _read_limited(self, resp: httpx.Response, limit: int) -> tuple[bytes, bool]:
        """(본문, 잘렸는지). limit를 넘는 응답은 거기까지만 읽고 멈춘다."""
        chunks, size = [], 0
        for chunk in resp.iter_bytes():
            size += len(chunk)
            if size > limit:
                return b"".join(chunks), True
            chunks.append(chunk)
        return b"".join(chunks), False

    def _throttle(self, url: str) -> None:
        self.throttle.wait(urlsplit(url).hostname or "")

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


def same_host(a: str, b: str) -> bool:
    return (urlsplit(a).hostname or "").lower() == (urlsplit(b).hostname or "").lower()


def _root(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


# EUC-KR로 선언해도 실제로는 확장 완성형(cp949: 똠·햏 등)을 쓰는 곳이 많다
_CHARSET_ALIASES = {"euc-kr": "cp949", "euc_kr": "cp949", "ks_c_5601-1987": "cp949", "ksc5601": "cp949", "x-windows-949": "cp949"}
_META_CHARSET_RE = re.compile(r"""<meta[^>]+charset\s*=\s*["']?\s*([a-z0-9_.:-]+)""", re.I)


def _decode(body: bytes, resp: httpx.Response) -> str:
    """응답 본문의 문자 인코딩 판정. 순서: BOM → 올바른 UTF-8 → 헤더 charset → <meta charset> → (선언이 없으면) CP949.

    - 비 ASCII 글자를 담은 바이트열이 UTF-8 규칙에 맞으면 사실상 UTF-8이다 (CP949·Latin-1 한글 바이트는 UTF-8로 풀리지
      않는다). 그래서 선언보다 먼저 본다. 헤더는 Latin-1(Tomcat·PHP 기본값)인데 본문은 UTF-8인 서버에서 "카드뉴스"가
      "ì¹´ëë´ì¤"로 깨지거나, 헤더와 meta가 서로 다른 인코딩을 말하는 서버에서 틀린 쪽을 고르는 일을 막는다.
    - 헤더에 charset이 없으면 httpx의 resp.encoding은 UTF-8을 기본값으로 돌려주므로 쓰지 않는다 (meta를 못 보게 된다).
    - 선언이 모두 맞지 않으면 첫 선언으로 읽고 깨진 글자만 바꾼다 — UTF-8 페이지에 잘못된 바이트 하나가 섞였다고
      CP949로 통째로 잘못 읽지 않게. 아무 선언이 없을 때만 CP949로 추측한다.
    """
    if body.startswith(b"\xef\xbb\xbf"):
        return body[3:].decode("utf-8", errors="replace")
    head = body[:4096].decode("ascii", errors="ignore")
    meta = _META_CHARSET_RE.search(head)
    declared = [enc for enc in (_charset(resp.charset_encoding), _charset(meta.group(1) if meta else None))
                if enc and _known(enc)]
    for enc in dict.fromkeys(["utf-8"] + declared):
        try:
            return body.decode(enc)
        except UnicodeDecodeError:
            continue
    if declared:
        return body.decode(declared[0], errors="replace")
    loose = body.decode("utf-8", errors="replace")
    if loose.count("�") * 10 < sum(1 for ch in loose if ord(ch) > 127):
        return loose   # 대부분 UTF-8이고 몇 바이트만 깨졌다
    try:
        return body.decode("cp949")
    except UnicodeDecodeError:
        return loose


def _known(enc: str) -> bool:
    try:
        codecs.lookup(enc)
        return True
    except LookupError:
        return False


def _charset(name: str | None) -> str | None:
    if not name:
        return None
    name = name.strip().strip("\"'").lower()
    return _CHARSET_ALIASES.get(name, name) or None


def client_redirect_target(page: Page) -> str | None:
    """작은 HTML 응답이 meta refresh·location.href로 넘기는 주소. 없으면 None."""
    if page.status != 200 or "html" not in page.content_type.lower() or len(page.text) > CLIENT_REDIRECT_MAX_BYTES:
        return None
    m = META_REFRESH_RE.search(page.text)
    if m:
        return m.group(1).strip()
    m = JS_REDIRECT_RE.search(page.text)
    if m:
        target = (m.group(1) or m.group(2) or "").strip()
        return target or None
    return None


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
    return any(isinstance(c, ssl.SSLError) for c in _causes(e)) or "ssl" in repr(e).lower() or "certificate" in repr(e).lower()


def _is_cert_error(e: Exception) -> bool:
    """인증서 검증 실패(만료·체인·호스트명 불일치)인지. httpx는 OpenSSL 메시지를 문자열로만 넘기기도 해서 본문도 본다."""
    if any(isinstance(c, ssl.SSLCertVerificationError) for c in _causes(e)):
        return True
    text = repr(e).lower()
    return "certificate_verify_failed" in text or "certificate verify failed" in text


def _causes(e: BaseException) -> list[BaseException]:
    out: list[BaseException] = []
    cur: BaseException | None = e
    while cur is not None and cur not in out and len(out) < 8:
        out.append(cur)
        cur = cur.__cause__ or cur.__context__
    return out


def _ssl_detail(e: Exception) -> str:
    return str(e).strip()[:300] or type(e).__name__
