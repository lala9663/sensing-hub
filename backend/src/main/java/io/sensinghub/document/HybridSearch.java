package io.sensinghub.document;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.TimeUnit;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import io.sensinghub.auth.CurrentUser;
import io.sensinghub.document.DocumentService.DocumentRow;
import io.sensinghub.document.DocumentService.Filter;
import io.sensinghub.document.DocumentService.SemanticMatch;
import io.sensinghub.llm.EmbeddingClient;
import io.sensinghub.llm.LlmException;

/**
 * 하이브리드 문서 검색 (M5 챗봇 2단계): 전문 검색(검색어 낱말이 모두 든 문서, {@link DocumentService#list} — 발행일 최신순)과
 * 의미 검색(질의 임베딩과 조각 벡터의 코사인 유사도순)에서 각각 후보를 뽑아 <b>RRF</b>(Reciprocal Rank Fusion: 문서마다
 * 1/(k + 순위)를 더한 점수)로 합친다. 두 목록에 다 있는 문서가 맨 앞에 오고, 낱말은 없지만 뜻이 가까운 문서
 * ('배드민턴' → '안세영 덴마크오픈 우승')가 뒤를 잇는다. 점수 대신 순위만 쓰므로 tsvector 일치와 코사인 유사도의 척도를 맞출 필요가 없다.
 * <p>의미 검색은 아래 경우 건너뛰고 지금까지의 전문 검색 결과를 그대로 준다 (검색이 실패하지 않는다):
 * 임베딩 미설정, 그 모델로 임베딩된 조각이 아직 없음(질의를 임베딩하지 않아 호출 한도를 아낀다), 질의 임베딩 실패(429·5xx·시간 초과 —
 * 그 뒤 {@code embed-cooldown-seconds} 동안은 부르지 않는다).
 * <p>질의가 비면 검색 없이 최신 문서를 준다.
 */
@Service
public class HybridSearch {

    private static final Logger log = LoggerFactory.getLogger(HybridSearch.class);

    public enum Mode {
        /** 검색어 없음: 조건에 맞는 최신 문서 */
        LATEST,
        /** 전문 검색만 (의미 검색 꺼짐·실패) */
        FULLTEXT,
        /** 전문 검색 + 의미 검색 (RRF) */
        HYBRID
    }

    /** words: 검색어 낱말이 모두 들어 있다. meaning: 뜻이 가깝다 (유사도 하한 이상). */
    public record Hit(DocumentRow row, boolean words, boolean meaning) {
    }

    /** note: 의미 검색을 하지 않은 까닭 (로그·디버깅용, HYBRID면 null). */
    public record Result(List<Hit> hits, Mode mode, String note) {
    }

    private record Semantic(List<SemanticMatch> matches, String note) {
    }

    private final DocumentService documents;
    private final EmbeddingClient embeddings;
    private final int candidates;
    private final int rrfK;
    private final double minSimilarity;
    private final long cooldownNanos;
    private volatile long pausedUntil;

    public HybridSearch(DocumentService documents, EmbeddingClient embeddings,
                        @Value("${sensing.search.candidates:50}") int candidates,
                        @Value("${sensing.search.rrf-k:60}") int rrfK,
                        @Value("${sensing.search.min-similarity:0.58}") double minSimilarity,
                        @Value("${sensing.search.embed-cooldown-seconds:60}") long cooldownSeconds) {
        this.documents = documents;
        this.embeddings = embeddings;
        this.candidates = Math.max(1, candidates);
        this.rrfK = Math.max(1, rrfK);
        this.minSimilarity = minSimilarity;
        this.cooldownNanos = TimeUnit.SECONDS.toNanos(Math.max(0, cooldownSeconds));
        this.pausedUntil = System.nanoTime();
    }

    /** 상태 표시용: "낱말+의미 (gemini-embedding-001/1024)" 또는 "낱말". */
    public String describe() {
        return embeddings.configured() ? "낱말+의미 (" + embeddings.identity() + ")" : "낱말";
    }

    /**
     * @param filter 목록과 같은 조건 (기간·분류·감시·기사 종류·사이트) — 전문·의미 후보 모두에 건다
     * @param limit 돌려줄 문서 수
     */
    public Result search(CurrentUser user, Filter filter, String q, int limit) {
        int n = Math.max(1, limit);
        if (q == null || q.isBlank()) {
            List<Hit> latest = documents.list(user, filter, null, n).stream()
                    .map(r -> new Hit(r, false, false)).toList();
            return new Result(latest, Mode.LATEST, null);
        }
        int pool = Math.max(n, candidates);
        List<DocumentRow> words = documents.list(user, filter, q, pool);
        Semantic semantic = semantic(user, filter, q.strip(), pool);
        if (semantic.matches() == null) {
            List<Hit> hits = words.stream().limit(n).map(r -> new Hit(r, true, false)).toList();
            return new Result(hits, Mode.FULLTEXT, semantic.note());
        }

        List<Long> wordIds = words.stream().map(DocumentRow::id).toList();
        List<Long> meaningIds = semantic.matches().stream().map(SemanticMatch::documentId).toList();
        List<Long> ranked = fuse(wordIds, meaningIds, rrfK).stream().limit(n).toList();
        Map<Long, DocumentRow> byId = new HashMap<>();
        words.forEach(r -> byId.put(r.id(), r));
        documents.rows(ranked.stream().filter(id -> !byId.containsKey(id)).toList()).forEach(r -> byId.put(r.id(), r));
        Set<Long> wordSet = new HashSet<>(wordIds);
        Set<Long> meaningSet = new HashSet<>(meaningIds);
        List<Hit> hits = ranked.stream().filter(byId::containsKey)
                .map(id -> new Hit(byId.get(id), wordSet.contains(id), meaningSet.contains(id))).toList();
        return new Result(hits, Mode.HYBRID, null);
    }

    private Semantic semantic(CurrentUser user, Filter filter, String q, int pool) {
        if (!embeddings.configured()) {
            return new Semantic(null, "의미 검색 꺼짐");
        }
        if (System.nanoTime() - pausedUntil < 0) {
            return new Semantic(null, "의미 검색 잠시 멈춤 (직전 질의 임베딩 실패)");
        }
        String model = embeddings.identity();
        if (!documents.hasEmbeddings(model)) {
            return new Semantic(null, "임베딩된 조각 없음 (" + model + ")");
        }
        try {
            float[] vector = embeddings.embed(q);
            return new Semantic(documents.semanticMatches(user, filter, vector, model, pool, minSimilarity),
                    null);
        } catch (LlmException e) {
            pause();
            log.warn("query embedding failed ({}), full-text only for {}s: {} {}", e.kind(),
                    TimeUnit.NANOSECONDS.toSeconds(cooldownNanos), e.getMessage(), e.detail() == null ? "" : e.detail());
            return new Semantic(null, "질의 임베딩 실패: " + e.kind());
        } catch (RuntimeException e) {   // 벡터 형식·DB 오류. 검색 자체는 전문 검색으로 계속한다
            pause();
            log.error("semantic search failed, full-text only", e);
            return new Semantic(null, "의미 검색 오류");
        }
    }

    private void pause() {
        pausedUntil = System.nanoTime() + cooldownNanos;
    }

    /** 테스트용: 실패 뒤 멈춤을 푼다. */
    void clearPause() {
        pausedUntil = System.nanoTime();
    }

    /**
     * RRF: 목록마다 1/(k + 순위)(순위는 1부터)를 더한 점수 높은 순. 같으면 더 좋은 순위, 그래도 같으면 낱말 목록 → 의미 목록에
     * 처음 나온 순서. k=60은 원 논문(Cormack et al., 2009)과 대부분 검색 엔진의 기본값이다.
     */
    static List<Long> fuse(List<Long> words, List<Long> meaning, int k) {
        Map<Long, double[]> scores = new LinkedHashMap<>();   // id → {점수, 가장 좋은 순위}
        for (List<Long> list : List.of(words, meaning)) {
            for (int i = 0; i < list.size(); i++) {
                int rank = i + 1;
                double[] s = scores.computeIfAbsent(list.get(i), id -> new double[] {0, Double.MAX_VALUE});
                s[0] += 1.0 / (k + rank);
                s[1] = Math.min(s[1], rank);
            }
        }
        List<Long> ids = new ArrayList<>(scores.keySet());
        ids.sort(Comparator.comparingDouble((Long id) -> -scores.get(id)[0]).thenComparingDouble(id -> scores.get(id)[1]));
        return ids;
    }
}
