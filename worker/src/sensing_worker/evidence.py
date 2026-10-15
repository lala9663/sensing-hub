"""실패 증거 저장: 차단·구조 변경을 판단한 응답 HTML 일부를 파일로 남긴다.

관리자가 "왜 BLOCKED/BROKEN이 됐는지"를 사이트에 다시 요청하지 않고 확인할 수 있게 한다.
파일은 {evidence_dir}/{YYYYMMDD}/job{작업ID}-{코드}-{임의값}.html 에 쓰고,
crawl_error.evidence_path에는 evidence_dir 기준 상대 경로를 기록한다 (컨테이너가 달라도 같은 값).
"""

from __future__ import annotations

import logging
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .codes import CrawlFailure

log = logging.getLogger(__name__)


class EvidenceStore:
    def __init__(self, root: str, retention_days: int = 30):
        self.root = Path(root)
        self.retention_days = retention_days

    def save(self, job_id: int, failure: CrawlFailure) -> str | None:
        """증거가 있으면 파일로 쓰고 상대 경로를 돌려준다. 디스크 오류는 수집 실패로 번지지 않게 삼킨다."""
        if not failure.evidence:
            return None
        now = datetime.now(timezone.utc)
        rel = Path(now.strftime("%Y%m%d")) / f"job{job_id}-{failure.code.value.lower()}-{uuid.uuid4().hex[:8]}.html"
        header = (
            "<!-- sensing-hub evidence\n"
            f"     url: {_comment_safe(failure.url or '')}\n"
            f"     code: {failure.code.value}  stage: {failure.stage}  http_status: {failure.http_status}\n"
            f"     saved_at: {now.isoformat(timespec='seconds')}\n"
            "-->\n"
        )
        try:
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(header + failure.evidence, encoding="utf-8")
        except OSError:
            log.warning("evidence write failed for job %s", job_id, exc_info=True)
            return None
        return rel.as_posix()

    def prune(self, now: datetime | None = None) -> int:
        """보관 기간이 지난 날짜 폴더를 지운다. 지운 폴더 수를 돌려준다."""
        if not self.root.is_dir():
            return 0
        cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=self.retention_days)).strftime("%Y%m%d")
        removed = 0
        for d in self.root.iterdir():
            if d.is_dir() and len(d.name) == 8 and d.name.isdigit() and d.name < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
        return removed


def _comment_safe(text: str) -> str:
    return text.replace("--", "%2D%2D").replace("\n", " ")
