"""테스트용 가짜 임베딩: 글마다 정해진 1024차원 벡터를 돌려주거나 EmbedUnavailable을 낸다. 실제 API는 호출하지 않는다."""

from __future__ import annotations

import zlib

from sensing_worker.embed import COLUMN_DIMENSIONS, EmbedUnavailable


def vector_for(text: str) -> list[float]:
    """글의 crc32로 고른 칸 하나만 1인 벡터 (같은 글은 같은 벡터)."""
    v = [0.0] * COLUMN_DIMENSIONS
    v[zlib.crc32(text.encode("utf-8")) % COLUMN_DIMENSIONS] = 1.0
    return v


class FakeEmbedder:
    provider = "fake"

    def __init__(self, model: str = "fake-embed", error: Exception | None = None):
        self.model = model
        self.error = error
        self.calls: list[list[str]] = []

    @property
    def identity(self) -> str:
        return f"{self.model}/{COLUMN_DIMENSIONS}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.error is not None:
            raise self.error
        return [vector_for(t) for t in texts]


def unavailable() -> EmbedUnavailable:
    return EmbedUnavailable("임베딩 서버 HTTP 429: quota")
