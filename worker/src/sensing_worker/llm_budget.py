"""LLM 무료 한도 나눠 쓰기: 사용량 장부(llm_usage)·쉬는 중(llm_cooldown)과 그것을 지키는 LLM 클라이언트 감싸기(MeteredLlm).

챗봇(백엔드)·자동 요약(enrich)·등록 레시피(recipe)가 같은 모델·같은 키의 하루 요청 수 한도를 나눠 쓴다 (V19, docs/decisions.md).
백엔드 io.sensinghub.llm.LlmLedger와 같은 테이블·같은 규칙이다.

- 날짜는 쿼터 기준 시간대(SENSING_LLM_QUOTA_TZ, 기본 America/Los_Angeles — Gemini 무료 한도는 태평양 시간 자정에 초기화)의
  날짜이고 DB 시계로 센다. 워커·백엔드 시계가 조금 달라도 같은 날짜·같은 "쉬는 끝 시각"을 쓴다.
- 호출 전에 calls를 하나 늘린다(예약). 하루 상한이 있는 용도(enrich, SENSING_ENRICH_DAILY_CALLS)는 상한 미만일 때만 늘리므로
  워커 여러 대가 합쳐도 넘지 않는다. 레시피·챗봇은 상한 없이 남은 한도를 쓴다.
- 429면 그 요청은 calls에서 빼 rate_limited로 옮기고 모델을 쉬는 중으로 적는다: 하루 한도 → 쿼터 기준 다음 자정과 retryDelay 중
  늦은 쪽까지, 그 밖(분당 한도·retry-after) → retryDelay(없으면 60초)만큼. 쉬는 동안은 부르지 않는다 (LlmPaused).
- 장부는 자기 연결(autocommit)을 쓴다. 보강은 문서를 잠근 트랜잭션 안에서 LLM을 부르므로, 같은 연결로 장부 행을 고치면
  호출이 끝날 때까지 행 잠금이 남아 다른 워커의 예약이 막힌다.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

from .llm import DAILY, LlmClient, LlmPaused, LlmRateLimited, LlmUnavailable, NoLlm, RateLimit

log = logging.getLogger(__name__)

CHAT = "chat"
ENRICH = "enrich"
RECIPE = "recipe"
EMBED = "embed"

CAP = "CAP"                       # Pause.reason: 이 용도의 하루 상한에 닿음 (쉬는 중 기록은 아니다)
DEFAULT_RETRY_SECONDS = 60        # 대기 시간을 알려 주지 않은 429
DISPLAY_TZ = "Asia/Seoul"         # 로그에 찍는 "한국 시간"
CONNECT_TIMEOUT_SECONDS = 5

_NEXT_RESET = "(((now() AT TIME ZONE %(tz)s)::date + 1)::timestamp AT TIME ZONE %(tz)s)"
_TODAY = "(now() AT TIME ZONE %(tz)s)::date"


@dataclass(frozen=True)
class Pause:
    """지금 이 모델(용도)을 부르지 않는다. reason: DAILY | MINUTE | RATE_LIMITED (llm_cooldown) | CAP (하루 상한)."""
    reason: str
    until: datetime
    until_kst: str                 # 'MM-DD HH:MI' (한국 시간, 로그용)
    quota_id: str | None = None
    calls: int | None = None       # CAP일 때 오늘 호출 수
    cap: int | None = None

    def describe(self) -> str:
        if self.reason == CAP:
            return f"오늘 호출 {self.calls}/{self.cap}번으로 하루 상한에 닿음"
        label = {"DAILY": "하루 한도", "MINUTE": "분당 한도"}.get(self.reason, "호출 한도")
        return label + (f" {self.quota_id}" if self.quota_id else "")


class LlmLedger:
    """llm_usage·llm_cooldown 읽기·쓰기. 연결이 끊기면 다음 호출에 다시 연결한다 (한 번 재시도)."""

    def __init__(self, dsn: str, quota_tz: str = "America/Los_Angeles"):
        self.dsn = dsn
        self.tz = quota_tz
        self._conn: psycopg.Connection | None = None
        self._lock = threading.Lock()
        try:   # 시간대 이름이 틀리면 시작할 때 바로 멈춘다 (make_llm처럼)
            self._one("SELECT " + _TODAY + " AS today", {"tz": self.tz})
        except psycopg.errors.InvalidParameterValue as e:
            raise RuntimeError(f"SENSING_LLM_QUOTA_TZ를 DB가 모르는 시간대입니다: {quota_tz!r}") from e

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _one(self, sql: str, params: Any) -> dict | None:
        with self._lock:
            for attempt in (0, 1):
                try:
                    if self._conn is None or self._conn.closed:
                        self._conn = psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row,
                                                     connect_timeout=CONNECT_TIMEOUT_SECONDS)
                    cur = self._conn.execute(sql, params)
                    return cur.fetchone() if cur.description else None
                except psycopg.OperationalError:
                    if self._conn is not None:
                        self._conn.close()
                    self._conn = None
                    if attempt:
                        raise
        return None   # pragma: no cover

    # ------------------------------------------------------------------ 사용량
    def reserve(self, model: str, purpose: str, cap: int | None = None) -> date | None:
        """호출 하나를 예약한다 (calls + 1). 오늘 이 용도의 호출이 이미 cap번이면 늘리지 않고 None. 돌려주는 값은 쿼터 기준 날짜."""
        row = self._one(f"""
            INSERT INTO llm_usage AS u (quota_day, model, purpose, calls, daily_cap)
            VALUES ({_TODAY}, %(model)s, %(purpose)s, 1, %(cap)s::int)
            ON CONFLICT (quota_day, model, purpose) DO UPDATE
                SET calls = u.calls + 1, daily_cap = EXCLUDED.daily_cap, updated_at = now()
                WHERE %(cap)s::int IS NULL OR u.calls < %(cap)s::int
            RETURNING u.quota_day
            """, {"tz": self.tz, "model": model, "purpose": purpose, "cap": cap})
        return row["quota_day"] if row else None

    def succeeded(self, day: date, model: str, purpose: str, input_tokens: int = 0, output_tokens: int = 0) -> None:
        self._one("""
            UPDATE llm_usage SET input_tokens = input_tokens + %s, output_tokens = output_tokens + %s, updated_at = now()
            WHERE quota_day = %s AND model = %s AND purpose = %s
            """, (int(input_tokens or 0), int(output_tokens or 0), day, model, purpose))

    def rate_limited(self, day: date, model: str, purpose: str) -> None:
        """429로 거절된 요청: 제공자 한도에 들어가지 않으므로 calls에서 빼 rate_limited로 옮긴다."""
        self._one("""
            UPDATE llm_usage SET calls = GREATEST(calls - 1, 0), rate_limited = rate_limited + 1, updated_at = now()
            WHERE quota_day = %s AND model = %s AND purpose = %s
            """, (day, model, purpose))

    def failed(self, day: date, model: str, purpose: str) -> None:
        self._one("UPDATE llm_usage SET errors = errors + 1, updated_at = now() "
                  "WHERE quota_day = %s AND model = %s AND purpose = %s", (day, model, purpose))

    def cap_reached(self, model: str, purpose: str, cap: int | None) -> Pause | None:
        """오늘 이 용도의 호출 수가 cap 이상이면 다음 리셋(쿼터 기준 자정)까지의 Pause. cap이 없으면 None."""
        if not cap:
            return None
        row = self._one(f"""
            SELECT COALESCE(u.calls, 0) AS calls, {_NEXT_RESET} AS reset_at,
                   to_char({_NEXT_RESET} AT TIME ZONE '{DISPLAY_TZ}', 'MM-DD HH24:MI') AS reset_kst
            FROM (SELECT 1) one
            LEFT JOIN llm_usage u ON u.quota_day = {_TODAY} AND u.model = %(model)s AND u.purpose = %(purpose)s
            """, {"tz": self.tz, "model": model, "purpose": purpose})
        if row["calls"] < cap:
            return None
        return Pause(CAP, row["reset_at"], row["reset_kst"], calls=row["calls"], cap=cap)

    # ------------------------------------------------------------------ 쉬는 중
    def cooldown(self, model: str) -> Pause | None:
        """이 모델이 지금 쉬는 중이면 그 Pause."""
        row = self._one(f"""
            SELECT reason, until_at, to_char(until_at AT TIME ZONE '{DISPLAY_TZ}', 'MM-DD HH24:MI') AS until_kst, quota_id
            FROM llm_cooldown WHERE model = %s AND until_at > now()
            """, (model,))
        return Pause(row["reason"], row["until_at"], row["until_kst"], row["quota_id"]) if row else None

    def cool_down(self, model: str, purpose: str, limit: RateLimit, detail: str = "") -> Pause | None:
        """429를 받았다: 한도 종류에 맞춰 쉬는 끝 시각을 적는다. 이미 더 늦게까지 쉬는 중이면 그대로 둔다. 지금의 Pause를 돌려준다."""
        self._one(f"""
            INSERT INTO llm_cooldown AS c (model, until_at, reason, quota_id, retry_seconds, purpose, detail, created_at)
            VALUES (%(model)s,
                    CASE WHEN %(reason)s = '{DAILY}'
                         THEN GREATEST(now() + make_interval(secs => COALESCE(%(retry)s::int, 0)), {_NEXT_RESET})
                         ELSE now() + make_interval(secs => COALESCE(%(retry)s::int, %(default)s::int)) END,
                    %(reason)s, %(quota_id)s, %(retry)s::int, %(purpose)s, %(detail)s, now())
            ON CONFLICT (model) DO UPDATE
                SET until_at = EXCLUDED.until_at, reason = EXCLUDED.reason, quota_id = EXCLUDED.quota_id,
                    retry_seconds = EXCLUDED.retry_seconds, purpose = EXCLUDED.purpose, detail = EXCLUDED.detail,
                    created_at = now()
                WHERE c.until_at < EXCLUDED.until_at
            """, {"tz": self.tz, "model": model, "reason": limit.scope, "quota_id": (limit.quota_id or "")[:200] or None,
                  "retry": limit.retry_seconds, "default": DEFAULT_RETRY_SECONDS, "purpose": purpose,
                  "detail": (detail or "")[:1000]})
        return self.cooldown(model)


class MeteredLlm:
    """LlmClient 감싸기: 부르기 전에 쉬는 중·하루 상한을 보고(걸리면 부르지 않고 LlmPaused), 부른 뒤 장부에 적는다.

    provider·model·last_usage는 감싼 클라이언트 그대로라 describe()·generate_recipe가 그대로 쓴다. 같은 클라이언트를 용도별로
    여러 번 감싼다 (레시피·보강).
    """

    def __init__(self, inner: LlmClient, ledger: LlmLedger, purpose: str, daily_cap: int | None = None):
        self.inner = inner
        self.ledger = ledger
        self.purpose = purpose
        self.daily_cap = daily_cap if daily_cap and daily_cap > 0 else None
        self._noticed: tuple | None = None

    @property
    def provider(self) -> str:
        return getattr(self.inner, "provider", type(self.inner).__name__)

    @property
    def model(self) -> str:
        return getattr(self.inner, "model", "")

    @property
    def last_usage(self) -> dict[str, int] | None:
        return getattr(self.inner, "last_usage", None)

    def paused(self) -> Pause | None:
        """지금 부르면 안 되는가 (모델이 쉬는 중이거나 이 용도의 하루 상한에 닿음). 장부를 읽기만 한다.
        쉬기 시작하거나 다시 풀릴 때만 로그를 남긴다 (루프마다 찍지 않는다)."""
        pause = self.ledger.cooldown(self.model) or self.ledger.cap_reached(self.model, self.purpose, self.daily_cap)
        self._notice(pause)
        return pause

    def _notice(self, pause: Pause | None) -> None:
        key = (pause.reason, pause.until) if pause else None
        if key == self._noticed:
            return
        if pause is not None:
            log.warning("llm %s paused for %s until %s KST (%s); no calls until then", self.model, self.purpose,
                        pause.until_kst, pause.describe())
        else:
            log.info("llm %s resumed for %s", self.model, self.purpose)
        self._noticed = key

    def complete(self, system: str, user: str, schema: dict) -> str:
        try:
            pause = self.ledger.cooldown(self.model)
            day = None if pause else self.ledger.reserve(self.model, self.purpose, self.daily_cap)
            if pause is None and day is None:
                pause = self.ledger.cap_reached(self.model, self.purpose, self.daily_cap)
        except psycopg.Error as e:
            raise LlmUnavailable(f"LLM 사용량 장부를 읽지 못했습니다: {type(e).__name__}: {e}"[:300]) from e
        if pause is not None or day is None:
            if pause is not None:
                self._notice(pause)
            raise LlmPaused(f"LLM 호출을 쉬는 중입니다 ({pause.describe() if pause else '하루 상한'})", pause)
        if hasattr(self.inner, "last_usage"):
            self.inner.last_usage = None   # 이번 호출이 사용량을 주지 않으면 지난 호출 값을 두 번 세지 않게
        try:
            text = self.inner.complete(system, user, schema)
        except LlmRateLimited as e:
            self._write(self.ledger.rate_limited, day, self.model, self.purpose)
            try:
                pause = self.ledger.cool_down(self.model, self.purpose, e.limit, str(e))
            except psycopg.Error as db_error:
                log.warning("llm cooldown not recorded: %s", db_error)
            else:
                self._notice(pause)
            raise
        except Exception:
            self._write(self.ledger.failed, day, self.model, self.purpose)
            raise
        usage = self.last_usage or {}
        self._write(self.ledger.succeeded, day, self.model, self.purpose,
                    usage.get("input_tokens") or 0, usage.get("output_tokens") or 0)
        return text

    @staticmethod
    def _write(action, *args) -> None:
        """호출은 이미 끝났다: 장부를 못 적어도 결과는 돌려준다."""
        try:
            action(*args)
        except psycopg.Error as e:
            log.warning("llm usage not recorded: %s", e)


def metered(llm: LlmClient, ledger: LlmLedger | None, purpose: str, daily_cap: int | None = None) -> LlmClient:
    """장부가 있고 LLM이 설정돼 있으면 MeteredLlm으로 감싼다. NoLlm은 그대로 둔다 (부르지 않으므로 셀 것도 없다)."""
    if ledger is None or isinstance(llm, NoLlm):
        return llm
    return MeteredLlm(llm, ledger, purpose, daily_cap)
