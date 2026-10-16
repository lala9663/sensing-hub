package io.sensinghub.digest;

import static org.assertj.core.api.Assertions.assertThat;

import java.time.OffsetDateTime;
import java.util.List;
import java.util.Set;

import org.junit.jupiter.api.Test;

import io.sensinghub.digest.DigestComposer.Digest;
import io.sensinghub.digest.DigestComposer.Document;
import io.sensinghub.digest.DigestComposer.WatchHit;

/** DB 없이 본문 묶음·형식을 확인한다. */
class DigestComposerTest {

    private static final OffsetDateTime FROM = OffsetDateTime.parse("2026-10-15T08:00:00+09:00");
    private static final OffsetDateTime TO = OffsetDateTime.parse("2026-10-16T08:00:00+09:00");

    private final DigestComposer composer = new DigestComposer();

    private static Document doc(long id, String title, String domain, String published, String summary,
                                List<String> topics, WatchHit... hits) {
        return new Document(id, "https://" + domain + "/" + id, title,
                published == null ? null : OffsetDateTime.parse(published), domain, summary, topics, List.of(hits));
    }

    @Test
    void groupsWatchHitsThenTopicsInTaxonomyOrderThenUnclassified() {
        List<Document> docs = List.of(
                doc(1, "메타넷, 신제품 공개", "news.example.com", "2026-10-16T09:00:00+09:00", null,
                        List.of("자사", "산업동향"), new WatchHit(7, "자사 보도", "메타넷")),
                doc(2, "A사, 메타넷 <추격>", "news.example.com", "2026-10-16T08:30:00+09:00", null,
                        List.of("경쟁사"), new WatchHit(7, "자사 보도", "메타넷"), new WatchHit(8, "경쟁사", "A사")),
                doc(3, "2026년 지원사업 공고", "gov.example.go.kr", "2026-10-14T09:00:00+09:00",
                        "중소벤처기업부가 10월 31일까지 신청을 받는다.", List.of("지원사업", "정책·규제")),
                doc(4, "아직 보강 전", "news.example.com", null, null, List.of()));

        Digest d = composer.compose("아침 보고", FROM, TO, docs, Set.of(), false);

        assertThat(d.documentCount()).isEqualTo(4);
        assertThat(d.subject()).isEqualTo("[sensing-hub] 아침 보고 다이제스트 2026-10-16 (4건)");
        String text = d.text();
        assertThat(text).startsWith("*sensing-hub 일간 다이제스트 — 아침 보고*\n기간: 2026-10-15 08:00 ~ 2026-10-16 08:00 (KST) · 새 문서 4건\n");
        assertThat(text).endsWith("\n— sensing-hub 다이제스트\n");
        // 묶음 순서: 감시(id 순, 검색어별) → 토픽(체계 순서) → 분류 전. 감시에 나온 문서는 토픽 묶음에 다시 나오지 않는다
        assertThat(indexes(text, "*감시: 자사 보도*", "_메타넷_", "*감시: 경쟁사*", "_A사_", "*정책·규제*", "*지원사업*", "*분류 전*"))
                .isSorted();
        assertThat(text).doesNotContain("*자사*").doesNotContain("*산업동향*").doesNotContain("*경쟁사*\n");
        assertThat(text).contains("- 메타넷, 신제품 공개 — news.example.com · 2026-10-16 09:00 · https://news.example.com/1\n");
        assertThat(text).contains("- 2026년 지원사업 공고 — gov.example.go.kr · 2026-10-14 09:00 · https://gov.example.go.kr/3\n"
                + "  중소벤처기업부가 10월 31일까지 신청을 받는다.\n");
        assertThat(text).contains("- 아직 보강 전 — news.example.com · 발행일 없음 · https://news.example.com/4\n");
        // 토픽이 둘인 문서는 두 토픽에 모두 나온다
        assertThat(text.split("2026년 지원사업 공고", -1)).hasSize(3);
        // 언론 기사(요약 null)는 요약 줄이 없다
        assertThat(text).doesNotContain("메타넷, 신제품 공개 —\n  ");

        String html = d.html();
        assertThat(html).contains("<h3 style=\"margin:16px 0 4px;border-bottom:1px solid #ddd\">감시: 자사 보도</h3>")
                .contains("<a href=\"https://news.example.com/1\">메타넷, 신제품 공개</a>")
                .contains("A사, 메타넷 &lt;추격&gt;")                                          // HTML 이스케이프
                .contains("<br><span>중소벤처기업부가 10월 31일까지 신청을 받는다.</span>")
                .contains("새 문서 <b>4건</b>")
                .contains("— sensing-hub 다이제스트");
    }

    @Test
    void watchFilterLimitsWatchGroupsAndEmptyListStillHasHeader() {
        List<Document> docs = List.of(
                doc(2, "A사 소식", "news.example.com", "2026-10-16T08:30:00+09:00", null, List.of("경쟁사"),
                        new WatchHit(7, "자사 보도", "메타넷"), new WatchHit(8, "경쟁사", "A사")));
        Digest d = composer.compose("경쟁사만", FROM, TO, docs, Set.of(8L), true);
        assertThat(d.text()).contains("*감시: 경쟁사*").doesNotContain("자사 보도").contains("(상한에 걸려 최근 1건만)");

        Digest empty = composer.compose("빈 날", FROM, TO, List.of(), Set.of(), false);
        assertThat(empty.documentCount()).isZero();
        assertThat(empty.text()).contains("새 문서 0건").endsWith("— sensing-hub 다이제스트\n");
    }

    private static List<Integer> indexes(String text, String... needles) {
        List<Integer> out = new java.util.ArrayList<>();
        for (String n : needles) {
            int i = text.indexOf(n);
            assertThat(i).as("본문에 %s가 있어야 한다", n).isNotNegative();
            out.add(i);
        }
        return out;
    }
}
