"""워커 진입점: 큐에서 작업을 가져와 처리한다.

    sensing-worker            # 계속 실행
    sensing-worker --once     # 대기 작업 하나만 처리 (테스트·디버깅용)
"""

from __future__ import annotations

import argparse
import logging
import threading
import time

from .codes import Code, CrawlFailure
from .config import Settings
from .db import Store
from .fetcher import PoliteFetcher
from .jobs import JobRunner
from .throttle import DbThrottle

log = logging.getLogger("sensing_worker")


def process_one(store: Store, runner: JobRunner, settings: Settings) -> str | None:
    job = store.claim_job(settings.worker_id, settings.lease_minutes)
    if job is None:
        return None
    log.info("job %s claimed: %s source=%s attempt=%s", job.id, job.job_type, job.source_id, job.attempt)

    stop = threading.Event()

    def heartbeat() -> None:
        # 긴 작업 중에도 임대를 연장해 스케줄러가 회수하지 않게 한다
        hb = Store(settings.db_url)
        try:
            while not stop.wait(max(30.0, settings.lease_minutes * 60 / 3)):
                hb.extend_lease(job.id, settings.worker_id, settings.lease_minutes)
        finally:
            hb.close()

    t = threading.Thread(target=heartbeat, daemon=True)
    t.start()
    try:
        status = runner.run(job)
    except Exception as e:  # 분류되지 않은 오류도 작업을 끝내고 기록한다
        log.exception("job %s crashed", job.id)
        store.record_error(job.id, CrawlFailure(Code.UNEXPECTED, f"{type(e).__name__}: {e}"[:1000], stage="LIST"))
        store.finish_job(job.id, "FAILED", errors=1)
        status = "FAILED"
    finally:
        stop.set()
    log.info("job %s finished: %s", job.id, status)
    return status


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="sensing-hub 수집 워커")
    parser.add_argument("--once", action="store_true", help="대기 작업 하나만 처리하고 종료")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings()
    store = Store(settings.db_url)
    throttle = DbThrottle(settings.db_url, settings.min_interval_seconds) if settings.shared_throttle else None
    fetcher = PoliteFetcher(settings, throttle=throttle)
    runner = JobRunner(store, fetcher, settings)
    log.info("worker %s started", settings.worker_id)
    try:
        while True:
            status = process_one(store, runner, settings)
            if args.once:
                break
            if status is None:
                time.sleep(settings.poll_seconds)
    finally:
        fetcher.close()
        store.close()


if __name__ == "__main__":
    main()
