package io.sensinghub.document;

import static org.assertj.core.api.Assertions.assertThat;

import java.util.List;

import org.junit.jupiter.api.Test;

/** RRF 합치기: 두 목록에 다 있는 문서가 앞, 같은 점수는 더 좋은 순위 → 낱말 목록 순서. */
class HybridSearchFuseTest {

    @Test
    void documentsInBothListsComeFirstThenTiesKeepWordsBeforeMeaning() {
        // 낱말(최신순): 1, 2, 3   의미(유사도순): 9, 8, 2
        List<Long> fused = HybridSearch.fuse(List.of(1L, 2L, 3L), List.of(9L, 8L, 2L), 60);
        // 2 = 1/62 + 1/63, 1 = 9 = 1/61 (1위끼리 동점 → 낱말 목록 먼저), 8 = 1/62, 3 = 1/63
        assertThat(fused).containsExactly(2L, 1L, 9L, 8L, 3L);
    }

    @Test
    void emptyListsAndSmallK() {
        assertThat(HybridSearch.fuse(List.of(), List.of(), 60)).isEmpty();
        assertThat(HybridSearch.fuse(List.of(5L, 6L), List.of(), 60)).containsExactly(5L, 6L);
        assertThat(HybridSearch.fuse(List.of(), List.of(7L, 6L), 60)).containsExactly(7L, 6L);
        // k=1: 양쪽 2위(1/3 + 1/3)가 한쪽 1위(1/2)보다 앞선다
        assertThat(HybridSearch.fuse(List.of(1L, 2L), List.of(3L, 2L), 1)).containsExactly(2L, 1L, 3L);
    }
}
