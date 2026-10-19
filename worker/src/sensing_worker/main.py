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
from .enrich import enrich_pending
from .fetcher import PoliteFetcher
from .heartbeat import WorkerHeartbeat
from .indexer import index_pending
from .jobs import JobRunner
from .llm import NoLlm, describe, make_llm
from .throttle import DbThrottle

log = logging.getLogger("sensing_worker")


def process_one(store: Store, runner: JobRunner, settings: Settings,
                alive: WorkerHeartbeat | None = None) -> str | None:
    job = store.claim_job(settings.worker_id, settings.lease_minutes)
    if job is None:
        return None
    log.info("job %s claimed: %s source=%s attempt=%s", job.id, job.job_type, job.source_id, job.attempt)
    if alive is not None:
        alive.set_job(job.id)

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
        if alive is not None:
            alive.set_job(None)
    log.info("job %s finished: %s", job.id, status)
    return status


def enrich_idle(store: Store, llm, settings: Settings) -> None:
    """큐가 비었을 때 한 묶음 보강한다. 보강의 어떤 오류도 수집 루프를 멈추지 않는다."""
    if not settings.enrich_enabled or isinstance(llm, NoLlm):
        return
    try:
        enrich_pending(store, llm, settings, settings.enrich_batch)
    except Exception:   # DB 오류 등. 다음 루프에 다시 시도한다
        log.exception("enrich failed")


def index_idle(store: Store, settings: Settings) -> None:
    """큐가 비었을 때 한 묶음 색인하고 이슈로 묶는다 (LLM과 무관). 어떤 오류도 수집 루프를 멈추지 않는다."""
    if not settings.index_enabled:
        return
    try:
        index_pending(store, settings, settings.index_batch)
    except Exception:   # DB 오류 등. 트랜잭션은 되돌려졌고 다음 루프에 다시 시도한다
        log.exception("index failed")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="sensing-hub 수집 워커")
    parser.add_argument("--once", action="store_true", help="대기 작업 하나만 처리하고 종료")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings()
    store = Store(settings.db_url, onboard_ratio=settings.onboard_ratio)
    throttle = DbThrottle(settings.db_url, settings.min_interval_seconds) if settings.shared_throttle else None
    fetcher = PoliteFetcher(settings, throttle=throttle)
    llm = make_llm(settings)   # 제공자 설정이 잘못됐으면(패키지 없음·주소 없음) 여기서 바로 멈춘다
    runner = JobRunner(store, fetcher, settings, llm=llm)
    pruned = runner.evidence.prune()
    log.info("worker %s started (llm: %s, evidence dirs pruned: %s)", settings.worker_id, describe(llm), pruned)
    alive = WorkerHeartbeat(settings.db_url, settings.worker_id, settings.heartbeat_seconds)
    alive.start()   # 큐가 비어도 운영 지표에 살아 있는 워커로 보이게 30초마다 신호를 남긴다
    try:
        while True:
            status = process_one(store, runner, settings, alive)
            if args.once:
                break
            if status is None:
                index_idle(store, settings)         # 작업이 없을 때만 색인·이슈 묶기와
                enrich_idle(store, llm, settings)   # 요약·분류를 한 묶음씩 돌린다
                time.sleep(settings.poll_seconds)
    finally:
        alive.stop()
        fetcher.close()
        store.close()


if __name__ == "__main__":
    main()
