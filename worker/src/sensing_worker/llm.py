"""LLM 클라이언트 추상화: 등록 시 사이트 수집 레시피를 모델에게 쓰게 할 때 쓴다.

제공자는 환경 변수(SENSING_LLM_PROVIDER)로 고른다.
- none      : LLM을 쓰지 않는다 (기본). 휴리스틱 탐색만 한다.
- anthropic : Claude API (공식 anthropic SDK, 선택 의존성 llm-anthropic)
- openai    : OpenAI 호환 chat/completions API (vLLM·Ollama 등 사내 반입 모델). httpx만 쓴다.

구현체는 모델이 낸 원문 텍스트만 돌려준다. JSON 꺼내기·스키마 검증·재요청은 이 모듈의 공통 코드(generate_recipe)가
하고, 제공자가 스키마 강제를 지원하면 힌트로만 쓴다. 모델 출력은 문자열 데이터일 뿐이며 절대 실행하지 않는다.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from .config import Settings

log = logging.getLogger(__name__)

ANTHROPIC_DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LlmUnavailable(Exception):
    """LLM을 쓸 수 없다 (미설정·연결 실패·HTTP 오류·거절). 등록은 휴리스틱으로 계속한다."""


class LlmClient(Protocol):
    def complete(self, system: str, user: str, schema: dict) -> str:
        """시스템·사용자 프롬프트와 기대하는 JSON 스키마를 받아 모델의 원문 텍스트를 돌려준다."""
        ...


class NoLlm:
    provider = "none"
    model = ""

    def complete(self, system: str, user: str, schema: dict) -> str:
        raise LlmUnavailable("LLM이 설정되지 않았습니다 (SENSING_LLM_PROVIDER=none).")


# ---------------------------------------------------------------------- Anthropic (Claude)

def _anthropic_errors() -> tuple[type[Exception], ...]:
    try:
        import anthropic  # type: ignore
    except ImportError:  # pragma: no cover - 클라이언트를 주입한 테스트에서만 지나간다
        return ()
    return (anthropic.APIStatusError, anthropic.APIConnectionError)


class AnthropicLlm:
    """공식 anthropic SDK(1.x). 키는 ANTHROPIC_API_KEY, 게이트웨이는 ANTHROPIC_BASE_URL 환경 변수로 준다."""

    provider = "anthropic"

    def __init__(self, model: str = ANTHROPIC_DEFAULT_MODEL, *, fallbacks: bool = True, timeout: float = 120.0,
                 client: Any | None = None):
        self.model = model or ANTHROPIC_DEFAULT_MODEL
        self.fallbacks = fallbacks
        self.last_usage: dict[str, int] | None = None
        if client is None:
            try:
                import anthropic  # type: ignore
            except ImportError as e:
                raise RuntimeError("SENSING_LLM_PROVIDER=anthropic에는 anthropic 패키지가 필요합니다: "
                                   "pip install -e \".[llm-anthropic]\"") from e
            client = anthropic.Anthropic(timeout=timeout)
        self.client = client

    def complete(self, system: str, user: str, schema: dict) -> str:
        kwargs: dict[str, Any] = {
            "model": self.model, "max_tokens": 16000, "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_config": {"format": {"type": "json_schema", "schema": schema}},
        }
        try:
            if self.fallbacks:
                # 안전 분류기가 거절하면 서버가 거절 범주에 맞는 다른 모델로 같은 요청을 이어서 처리한다.
                # fallbacks·betas는 beta 네임스페이스(client.beta.messages)에서만 받는다
                response = self.client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
            else:
                response = self.client.messages.create(**kwargs)
        except _anthropic_errors() as e:
            raise LlmUnavailable(f"Claude API 오류: {type(e).__name__}: {e}"[:500]) from e
        usage = getattr(response, "usage", None)
        if usage is not None:
            self.last_usage = {"input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
                               "output_tokens": int(getattr(usage, "output_tokens", 0) or 0)}
        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            raise LlmUnavailable(f"Claude가 요청을 거절했습니다 ({getattr(details, 'category', None)})")
        for block in getattr(response, "content", None) or []:
            if getattr(block, "type", None) == "text":
                return block.text or ""
        return ""


# ---------------------------------------------------------------------- OpenAI 호환 (vLLM·Ollama·...)

# response_format 단계: 서버가 400으로 거부하면 다음 단계로 내려가고, 내려간 단계를 기억한다
_FORMAT_STEPS = ("json_schema", "json_object", "none")


class OpenAiCompatLlm:
    """{base_url}/chat/completions 에 POST. 추가 의존성 없이 httpx만 쓴다."""

    provider = "openai"

    def __init__(self, base_url: str, model: str, api_key: str = "", *, timeout: float = 120.0,
                 transport: httpx.BaseTransport | None = None):
        if not base_url:
            raise RuntimeError("SENSING_LLM_PROVIDER=openai에는 SENSING_LLM_BASE_URL이 필요합니다 (예: http://localhost:11434/v1)")
        if not model:
            raise RuntimeError("SENSING_LLM_PROVIDER=openai에는 SENSING_LLM_MODEL이 필요합니다 (서버에 올린 모델 이름)")
        self.base_url = base_url.rstrip("/")
        self.model = model
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.Client(headers=headers, timeout=timeout, transport=transport)
        self._format_step = 0
        self.last_usage: dict[str, int] | None = None

    def close(self) -> None:
        self._client.close()

    def _body(self, system: str, user: str, schema: dict, step: str) -> dict:
        body: dict[str, Any] = {
            "model": self.model, "temperature": 0,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if step == "json_schema":
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "recipe", "schema": schema, "strict": True}}
        elif step == "json_object":
            body["response_format"] = {"type": "json_object"}
        return body

    def complete(self, system: str, user: str, schema: dict) -> str:
        url = f"{self.base_url}/chat/completions"
        while True:
            step = _FORMAT_STEPS[self._format_step]
            try:
                response = self._client.post(url, json=self._body(system, user, schema, step))
            except httpx.HTTPError as e:
                raise LlmUnavailable(f"LLM 서버 연결 실패 ({url}): {type(e).__name__}: {e}"[:500]) from e
            if response.status_code == 400 and self._format_step < len(_FORMAT_STEPS) - 1:
                # 이 서버는 이 response_format을 모른다: 한 단계 낮춰 다시 시도한다
                log.info("LLM server rejected response_format=%s, falling back: %s", step, response.text[:200])
                self._format_step += 1
                continue
            if response.status_code >= 400:
                raise LlmUnavailable(f"LLM 서버 HTTP {response.status_code}: {response.text[:300]}")
            try:
                data = response.json()
                content = data["choices"][0]["message"]["content"]
            except (ValueError, KeyError, IndexError, TypeError) as e:
                raise LlmUnavailable(f"LLM 서버 응답 형식을 해석할 수 없습니다: {response.text[:300]}") from e
            usage = data.get("usage") if isinstance(data, dict) else None
            if isinstance(usage, dict):
                self.last_usage = {"input_tokens": int(usage.get("prompt_tokens") or 0),
                                   "output_tokens": int(usage.get("completion_tokens") or 0)}
            if isinstance(content, list):  # 일부 서버는 내용 블록 배열로 준다
                content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
            return content or ""


# ---------------------------------------------------------------------- 팩토리

def make_llm(settings: Settings) -> LlmClient:
    provider = (settings.llm_provider or "none").strip().lower()
    if provider in ("", "none", "off", "false"):
        return NoLlm()
    if provider == "anthropic":
        return AnthropicLlm(settings.llm_model, fallbacks=settings.llm_fallbacks, timeout=settings.llm_timeout_seconds)
    if provider in ("openai", "openai-compat", "openai_compat"):
        return OpenAiCompatLlm(settings.llm_base_url, settings.llm_model, settings.llm_api_key,
                               timeout=settings.llm_timeout_seconds)
    raise RuntimeError(f"알 수 없는 SENSING_LLM_PROVIDER: {settings.llm_provider!r} (none | anthropic | openai)")


def describe(llm: Any) -> str:
    """change_note·로그용: '제공자 모델'."""
    provider = getattr(llm, "provider", type(llm).__name__)
    model = getattr(llm, "model", "")
    return f"{provider} {model}".strip()


# ---------------------------------------------------------------------- JSON 꺼내기·스키마 검증

_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*\s*(.*?)```", re.S)


def extract_json(text: str) -> dict:
    """모델 응답에서 JSON 객체 하나를 꺼낸다. 코드 펜스나 앞뒤 설명이 붙어 있어도 첫 '{'부터 마지막 '}'까지를 해석한다."""
    if not text or not text.strip():
        raise ValueError("빈 응답")
    candidates = [m.group(1) for m in _FENCE_RE.finditer(text)] + [text]
    last_error: Exception | None = None
    for chunk in candidates:
        start, end = chunk.find("{"), chunk.rfind("}")
        if start < 0 or end <= start:
            continue
        try:
            data = json.loads(chunk[start:end + 1])
        except json.JSONDecodeError as e:
            last_error = e
            try:  # 닫는 중괄호 뒤에 설명이 더 붙은 경우: 첫 '{'에서 객체 하나만 읽는다
                data, _ = json.JSONDecoder().raw_decode(chunk, start)
            except json.JSONDecodeError:
                continue
        if isinstance(data, dict):
            return data
        last_error = ValueError(f"JSON 객체가 아닙니다: {type(data).__name__}")
    raise ValueError(f"응답에서 JSON 객체를 찾지 못했습니다: {last_error}")


_TYPE_CHECKS: dict[str, Callable[[Any], bool]] = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def validate_schema(data: Any, schema: dict, path: str = "$") -> list[str]:
    """jsonschema 없이 필요한 만큼만: type(단일·배열), required, properties, additionalProperties=false, items, enum."""
    errors: list[str] = []
    types = schema.get("type")
    if types is not None:
        allowed = types if isinstance(types, list) else [types]
        if not any(_TYPE_CHECKS.get(t, lambda v: False)(data) for t in allowed):
            return [f"{path}: {'|'.join(allowed)} 이어야 하는데 {type(data).__name__} 입니다"]
    if "enum" in schema and data not in schema["enum"]:
        errors.append(f"{path}: {schema['enum']} 중 하나여야 합니다")
    if isinstance(data, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in data:
                errors.append(f"{path}.{key}: 필수 키가 없습니다")
        if schema.get("additionalProperties") is False:
            for key in data:
                if key not in props:
                    errors.append(f"{path}.{key}: 허용되지 않는 키입니다")
        for key, sub in props.items():
            if key in data:
                errors += validate_schema(data[key], sub, f"{path}.{key}")
    if isinstance(data, list) and "items" in schema:
        for i, item in enumerate(data):
            errors += validate_schema(item, schema["items"], f"{path}[{i}]")
    return errors


# ---------------------------------------------------------------------- 공통 생성 루프

@dataclass
class Generation:
    data: dict | None
    attempts: int = 0
    errors: list[str] = field(default_factory=list)      # 마지막 시도의 검증 오류
    usage: dict[str, int] = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})
    raw: list[str] = field(default_factory=list)          # 시도별 원문 (로그·증거용)

    def usage_text(self) -> str:
        if not (self.usage["input_tokens"] or self.usage["output_tokens"]):
            return ""
        return f"토큰 입력 {self.usage['input_tokens']:,}·출력 {self.usage['output_tokens']:,}"


def generate_recipe(llm: LlmClient, system: str, user: str, schema: dict,
                    check: Callable[[dict], list[str]] | None = None, retries: int = 1) -> Generation:
    """complete() → JSON 꺼내기 → 스키마 검증 → (추가 검사). 실패하면 오류 메시지를 붙여 retries번 재요청한다.

    check: 스키마 밖의 빠른 검사 (셀렉터 문법, 서명 형식 등). 오류 메시지 목록을 돌려주면 재요청에 포함된다.
    LlmUnavailable은 그대로 올라간다 (호출자가 휴리스틱으로 돌아간다).
    """
    gen = Generation(data=None)
    prompt = user
    for attempt in range(retries + 1):
        gen.attempts = attempt + 1
        text = llm.complete(system, prompt, schema)
        gen.raw.append(text)
        usage = getattr(llm, "last_usage", None)
        if isinstance(usage, dict):
            gen.usage["input_tokens"] += int(usage.get("input_tokens") or 0)
            gen.usage["output_tokens"] += int(usage.get("output_tokens") or 0)
        try:
            data = extract_json(text)
        except ValueError as e:
            gen.errors = [str(e)]
        else:
            gen.errors = validate_schema(data, schema)
            if not gen.errors and check is not None:
                gen.errors = list(check(data))
            if not gen.errors:
                gen.data = data
                return gen
        log.info("LLM recipe attempt %s rejected: %s", attempt + 1, "; ".join(gen.errors)[:500])
        prompt = (user + "\n\n[이전 응답의 문제]\n" + "\n".join(f"- {e}" for e in gen.errors)
                  + "\n위 문제를 고쳐 스키마에 맞는 JSON 객체만 다시 출력하세요.")
    return gen
