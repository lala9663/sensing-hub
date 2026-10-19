"""의미 검색용 임베딩 (M5 챗봇 2단계): 전문 검색 색인(indexer.py)이 만든 조각(chunk)에 벡터를 채운다.

큐에 작업이 없을 때 main 루프가 색인 다음에 한 묶음(기본 20조각)씩 돌린다. 한 트랜잭션 안에서 ① 임베딩할 조각을 잠그고
(FOR UPDATE SKIP LOCKED) ② 한 번의 호출로 벡터를 받아 ③ chunk.embedding·embed_model을 채운다. 분당 보내는 조각 수(기본 80)를
넘지 않게 호출 사이를 띄우고(Pacer), 호출이 실패하면 아무것도 쓰지 않고 잠시 쉬었다가(실패가 이어지면 두 배씩, 최대 30분)
다시 시도한다. 백엔드는 질의를 같은 설정으로 임베딩해 전문 검색 결과와 합친다 (io.sensinghub.document.HybridSearch).

- 보내는 글: 언론 기사는 제목만 (색인 조각이 원래 제목 하나다). 공공기관·기타 문서는 제목 조각과 본문 조각이며, 본문 조각은
  앞에 제목을 붙여 보낸다 (조각만으로는 무슨 문서인지 모르는 경우가 많다). AI 봇을 막은 사이트(site.ai_restricted)는 본문 조각을
  외부 API로 보내지 않고 제목만 임베딩한다 — 챗봇이 그 사이트의 요약을 LLM에 보내지 않는 것과 같은 원칙이다.
- 어떤 모델로 만든 벡터인지 chunk.embed_model('모델/차원')에 남긴다. 모델을 바꾸면 기존 조각이 다시 대상이 되고, 백엔드는
  embed_model이 자기 설정과 같은 조각만 비교한다.
- 제공자는 OpenAI 호환 {base_url}/embeddings 하나다 (사내 vLLM·Ollama, OpenAI, Gemini의 OpenAI 호환 주소).
  Gemini 응답은 첫 항목의 index(0)를 빼고 준다 — 빠지면 순서대로 본다.

    sensing-embed-backfill [--limit N]     # 남은 조각을 한 번에 임베딩 (실제 API 호출이 조각 수 / 묶음 크기만큼 난다)
"""

from __future__ import annotations

import argparse
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .config import Settings

log = logging.getLogger(__name__)

# chunk.embedding VECTOR(1024) (V1)
COLUMN_DIMENSIONS = 1024
MAX_BACKOFF_SECONDS = 30 * 60


class EmbedUnavailable(Exception):
    """임베딩을 받을 수 없다 (연결 실패·HTTP 오류·응답 형식·차원 불일치). 아무것도 쓰지 않고 나중에 다시 시도한다."""


class Embedder(Protocol):
    provider: str
    model: str

    @property
    def identity(self) -> str | None:
        """chunk.embed_model에 남기는 값 ('모델/차원'). 미설정이면 None."""
        ...

    def embed(self, texts: list[str]) -> list[list[float]]:
        ...


class NoEmbedder:
    provider = "none"
    model = ""
    identity = None

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise EmbedUnavailable("임베딩이 설정되지 않았습니다 (SENSING_EMBED_PROVIDER=none).")


class OpenAiCompatEmbedder:
    """{base_url}/embeddings 에 POST. 추가 의존성 없이 httpx만 쓴다."""

    provider = "openai"

    def __init__(self, base_url: str, model: str, api_key: str = "", *, dimensions: int = COLUMN_DIMENSIONS,
                 timeout: float = 30.0, transport: httpx.BaseTransport | None = None):
        if not base_url:
            raise RuntimeError("SENSING_EMBED_PROVIDER=openai에는 SENSING_EMBED_BASE_URL(또는 SENSING_LLM_BASE_URL)이 필요합니다")
        if not model:
            raise RuntimeError("SENSING_EMBED_PROVIDER=openai에는 SENSING_EMBED_MODEL이 필요합니다 (예: gemini-embedding-001)")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.dimensions = dimensions
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.Client(headers=headers, timeout=timeout, transport=transport)
        # dimensions를 모르는 서버(1024차원 고정 모델을 올린 vLLM 등)는 400을 낸다: 한 번 빼고 다시 보내고 기억한다
        self._send_dimensions = True

    @property
    def identity(self) -> str:
        return f"{self.model}/{self.dimensions}"

    def close(self) -> None:
        self._client.close()

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        url = f"{self.base_url}/embeddings"
        while True:
            body: dict[str, Any] = {"model": self.model, "input": texts, "encoding_format": "float"}
            if self._send_dimensions:
                body["dimensions"] = self.dimensions
            try:
                response = self._client.post(url, json=body)
            except httpx.HTTPError as e:
                raise EmbedUnavailable(f"임베딩 서버 연결 실패 ({url}): {type(e).__name__}: {e}"[:500]) from e
            if response.status_code == 400 and self._send_dimensions:
                log.info("embedding server rejected dimensions=%s, retrying without: %s", self.dimensions,
                         response.text[:200])
                self._send_dimensions = False
                continue
            if response.status_code >= 400:
                raise EmbedUnavailable(f"임베딩 서버 HTTP {response.status_code}: {response.text[:300]}")
            return self._parse(response, len(texts))

    def _parse(self, response: httpx.Response, count: int) -> list[list[float]]:
        def position(pair: tuple[int, dict]) -> int:
            i, item = pair
            idx = item.get("index")
            return idx if isinstance(idx, int) else i

        try:
            items = response.json()["data"]
            vectors = [[float(x) for x in item["embedding"]] for _, item in sorted(enumerate(items), key=position)]
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            raise EmbedUnavailable(f"임베딩 응답 형식을 해석할 수 없습니다: {response.text[:300]}") from e
        if len(vectors) != count:
            raise EmbedUnavailable(f"임베딩 {count}개를 보냈는데 {len(vectors)}개가 왔습니다")
        for v in vectors:
            if len(v) != self.dimensions:
                raise EmbedUnavailable(
                    f"임베딩 차원이 {len(v)}입니다. chunk.embedding은 {self.dimensions}차원이라 dimensions를 받는 모델"
                    f"(Gemini·OpenAI text-embedding-3)이나 {self.dimensions}차원 모델(bge-m3 등)을 쓰세요")
        return vectors


def make_embedder(settings: Settings) -> Embedder:
    provider = (settings.embed_provider or "none").strip().lower()
    if provider in ("", "none", "off", "false"):
        return NoEmbedder()
    if provider in ("openai", "openai-compat", "openai_compat"):
        if settings.embed_dimensions != COLUMN_DIMENSIONS:
            raise RuntimeError(f"SENSING_EMBED_DIMENSIONS는 {COLUMN_DIMENSIONS}이어야 합니다 (chunk.embedding 열 크기)")
        own_url = settings.embed_base_url.strip()
        # 주소를 따로 주지 않았을 때만 LLM 키를 함께 쓴다 (LLM 키를 다른 서버에 보내지 않는다)
        base_url = own_url or settings.llm_base_url
        api_key = settings.embed_api_key or ("" if own_url else settings.llm_api_key)
        return OpenAiCompatEmbedder(base_url, settings.embed_model.strip(), api_key,
                                    dimensions=settings.embed_dimensions, timeout=settings.embed_timeout_seconds)
    raise RuntimeError(f"알 수 없는 SENSING_EMBED_PROVIDER: {settings.embed_provider!r} (none | openai)")


def describe(embedder: Any) -> str:
    identity = getattr(embedder, "identity", None)
    return f"{getattr(embedder, 'provider', type(embedder).__name__)} {identity or ''}".strip()


# ---------------------------------------------------------------------- 보낼 글

def _flat(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def embed_text(row: dict) -> str | None:
    """조각 한 행(lock_pending_embedding) → 임베딩할 글. None이면 외부로 보내지 않는 조각이다.

    제목 조각은 제목 그대로. 본문 조각은 '제목\\n조각'. 언론 기사(NEWS)와 AI 봇을 막은 사이트는 제목 조각만 보낸다.
    """
    title = _flat(row.get("title"))
    text = row.get("text") or ""
    is_title = row.get("seq") == 0 and bool(title) and _flat(text) == title
    if is_title:
        return title
    if row.get("site_type") == "NEWS" or row.get("ai_restricted"):
        return None
    if not text.strip():
        return None
    return f"{title}\n{text}" if title else text


# ---------------------------------------------------------------------- 실행

@dataclass
class EmbedOutcome:
    fetched: int = 0
    embedded: int = 0
    skipped: int = 0          # 외부로 보내지 않기로 한 조각 (embed_model만 채운다)
    unavailable: bool = False
    error: str | None = None


class Pacer:
    """임베딩 호출 간격. 두 가지를 지킨다.

    - 분당 글 수: 보낸 글 n개마다 60·n/per_minute초 뒤에 다음 호출 (0이면 제한 없음). Gemini 무료 등급은 묶음 안의 글 하나를
      요청 하나로 세어 분당 100개까지만 받는다 (2026-10-19 실측: 75개 묶음 한 번이 429).
    - 실패: base초 쉬고, 실패가 이어지면 두 배씩 늘린다 (최대 30분). 성공하면 처음으로. 하루 한도(429)에 걸렸을 때
      유휴 틱(5초)마다 거절당하는 호출을 보내지 않기 위해서다.
    """

    def __init__(self, per_minute: float, backoff_seconds: float, cap_seconds: float = MAX_BACKOFF_SECONDS):
        self.per_minute = max(0.0, per_minute)
        self.base = max(0.0, backoff_seconds)
        self.cap = cap_seconds
        self.failures = 0
        self.until = 0.0

    @staticmethod
    def _now(now: float | None) -> float:
        return time.monotonic() if now is None else now

    def ready(self, now: float | None = None) -> bool:
        return self._now(now) >= self.until

    def sent(self, count: int, now: float | None = None) -> None:
        """글 count개를 보내 성공했다."""
        self.failures = 0
        if self.per_minute > 0 and count > 0:
            self.until = self._now(now) + 60.0 * count / self.per_minute

    def failed(self, now: float | None = None) -> float:
        self.failures += 1
        wait = min(self.cap, self.base * 2 ** (self.failures - 1))
        self.until = self._now(now) + wait
        return wait


def _vector_literal(v: list[float]) -> str:
    return "[" + ",".join(repr(x) for x in v) + "]"


def embed_pending(store, embedder: Embedder, settings: Settings, limit: int | None = None) -> EmbedOutcome:
    """임베딩이 필요한 조각을 limit건 임베딩한다. 호출이 실패(EmbedUnavailable)하면 아무것도 쓰지 않고
    unavailable로 돌아간다 (잠금은 풀리고 다음에 다시 대상이 된다). 미설정(identity None)이면 아무것도 하지 않는다.
    """
    out = EmbedOutcome()
    identity = embedder.identity
    if identity is None:
        return out
    limit = limit or settings.embed_batch
    with store.conn.transaction():
        rows = store.lock_pending_embedding(identity, limit)
        out.fetched = len(rows)
        if not rows:
            return out
        texts: list[str] = []
        send: list[dict] = []
        skip: list[dict] = []
        for r in rows:
            t = embed_text(r)
            if t is None:
                skip.append(r)
            else:
                send.append(r)
                texts.append(t)
        vectors: list[list[float]] = []
        if texts:
            try:
                vectors = embedder.embed(texts)
            except EmbedUnavailable as e:
                out.unavailable = True
                out.error = str(e)
                return out
        with store.conn.cursor() as cur:
            if send:
                cur.executemany(
                    "UPDATE chunk SET embedding = CAST(%s AS vector), embed_model = %s, embedded_at = now() WHERE id = %s",
                    [(_vector_literal(v), identity, r["id"]) for r, v in zip(send, vectors)])
            if skip:
                cur.executemany(
                    "UPDATE chunk SET embedding = NULL, embed_model = %s, embedded_at = now() WHERE id = %s",
                    [(identity, r["id"]) for r in skip])
        out.embedded = len(send)
        out.skipped = len(skip)
    log.info("embed: chunks=%s embedded=%s skipped=%s model=%s", out.fetched, out.embedded, out.skipped, identity)
    return out


def backfill(store, embedder: Embedder, settings: Settings, limit: int | None = None,
             sleep=time.sleep) -> EmbedOutcome:
    """남은 조각이 없거나 호출이 실패할 때까지(또는 limit건까지) embed_pending을 되풀이한다. 분당 글 수 제한을 지킨다."""
    total = EmbedOutcome()
    pacer = Pacer(settings.embed_per_minute, 0)
    while limit is None or total.fetched < limit:
        if not pacer.ready():
            sleep(max(0.0, pacer.until - time.monotonic()))
        batch = settings.embed_batch if limit is None else min(settings.embed_batch, limit - total.fetched)
        out = embed_pending(store, embedder, settings, batch)
        pacer.sent(out.embedded)
        total.fetched += out.fetched
        total.embedded += out.embedded
        total.skipped += out.skipped
        if out.unavailable:
            total.unavailable, total.error = True, out.error
            break
        if out.fetched == 0:
            break
    return total


def main(argv: list[str] | None = None) -> None:
    from .db import Store

    parser = argparse.ArgumentParser(description="색인된 조각을 임베딩한다 (한 번 실행, SENSING_EMBED_* 설정 필요)")
    parser.add_argument("--limit", type=int, default=None, help="최대 조각 수 (기본: 남은 조각 전부)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings()
    embedder = make_embedder(settings)
    if embedder.identity is None:
        log.error("SENSING_EMBED_PROVIDER가 설정되지 않아 할 일이 없습니다")
        return
    store = Store(settings.db_url)
    try:
        out = backfill(store, embedder, settings, args.limit)
    finally:
        store.close()
    log.info("embed backfill: chunks=%s embedded=%s skipped=%s model=%s%s", out.fetched, out.embedded, out.skipped,
             embedder.identity, f" stopped: {out.error}" if out.unavailable else "")


if __name__ == "__main__":
    main()
