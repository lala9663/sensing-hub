package io.sensinghub.naver;

import java.util.ArrayDeque;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.function.LongSupplier;

/**
 * 사용자별 짧은 간격 제한 (네이버 검색 남용 방지): 최근 window 안에 허용한 호출이 limit개면 거절한다.
 * 검색 결과가 아니라 허용한 호출 시각만 메모리에 둔다 (사용자당 최대 limit개). 거절한 호출은 세지 않는다.
 */
final class UserRateLimiter {

    private final int limit;
    private final long windowNanos;
    private final LongSupplier clock;
    private final Map<Long, ArrayDeque<Long>> calls = new ConcurrentHashMap<>();

    UserRateLimiter(int limit, long windowMillis) {
        this(limit, windowMillis, System::nanoTime);
    }

    UserRateLimiter(int limit, long windowMillis, LongSupplier nanoClock) {
        this.limit = Math.max(1, limit);
        this.windowNanos = Math.max(1, windowMillis) * 1_000_000L;
        this.clock = nanoClock;
    }

    /** 허용하면 true (호출 시각을 남긴다), 간격이 너무 짧으면 false. */
    boolean tryAcquire(long userId) {
        long now = clock.getAsLong();
        ArrayDeque<Long> recent = calls.computeIfAbsent(userId, k -> new ArrayDeque<>());
        synchronized (recent) {
            while (!recent.isEmpty() && now - recent.peekFirst() >= windowNanos) {
                recent.pollFirst();
            }
            if (recent.size() >= limit) {
                return false;
            }
            recent.addLast(now);
            return true;
        }
    }

    int limit() {
        return limit;
    }
}
