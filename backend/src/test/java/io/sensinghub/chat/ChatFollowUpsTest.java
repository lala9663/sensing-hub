package io.sensinghub.chat;

import static org.assertj.core.api.Assertions.assertThat;

import java.util.ArrayList;
import java.util.List;

import org.junit.jupiter.api.Test;

import io.sensinghub.chat.ChatFollowUps.ActionFacts;
import io.sensinghub.chat.ChatFollowUps.DocFacts;
import io.sensinghub.chat.ChatFollowUps.Facts;
import io.sensinghub.chat.ChatFollowUps.FollowUp;
import io.sensinghub.chat.ChatFollowUps.IssueFacts;
import io.sensinghub.chat.ChatFollowUps.IssueItem;
import io.sensinghub.chat.ChatFollowUps.SearchFacts;
import io.sensinghub.chat.ChatFollowUps.WatchFacts;
import io.sensinghub.chat.ChatFollowUps.WatchItem;
import io.sensinghub.chat.ChatTools.Ref;

/** 추천 후속 질문 칩 규칙 (LLM 없이): 결과를 실제로 줄이는 값, 기간 넓히기, 0건, 앞 근거 안 0건, 이슈·감시·등록, 키워드 경로로 새지 않기. */
class ChatFollowUpsTest {

    private static DocFacts doc(long id, String source, List<String> topics, List<String> tags, List<String> companies) {
        return new DocFacts(id, source, null, topics, tags, companies);
    }

    private static Ref ref(int no, long documentId) {
        return new Ref(no, "DOCUMENT", documentId, null, "제목 " + no, "https://news.example.com/" + no, "예시일보", null);
    }

    private static SearchFacts search(String query, Integer days, boolean within, List<DocFacts> docs) {
        return new SearchFacts(query, null, List.of(), null, days, null, within, within ? 5 : 0, docs);
    }

    private static List<String> texts(List<FollowUp> chips) {
        return chips.stream().map(FollowUp::text).toList();
    }

    @Test
    void resultsSuggestNarrowingByCompanyTopicAndAWiderPeriod() {
        List<DocFacts> docs = List.of(
                doc(1, "예시일보", List.of("산업동향"), List.of("HBM"), List.of("삼성전자")),
                doc(2, "이데일리", List.of("정책·규제"), List.of("HBM"), List.of("삼성전자", "SK하이닉스")),
                doc(3, "이데일리", List.of("산업동향"), List.of("수출"), List.of()));
        List<Ref> cited = List.of(ref(1, 1), ref(2, 2), ref(3, 3));
        List<FollowUp> chips = ChatFollowUps.suggest(List.of(search("반도체", 7, false, docs)), cited);

        assertThat(texts(chips)).containsExactly("이 중 삼성전자 관련만", "이 중 산업동향만", "‘반도체’ 최근 30일로 넓혀 보기");
        assertThat(chips).allSatisfy(c -> assertThat(c.url()).isNull());
    }

    /** 근거로 쓴 문서만 본다: 답이 쓰지 않은 문서의 회사는 칩이 되지 않는다. 모든 근거에 있는 값(줄이지 못함)·검색어와 겹치는 값은 뺀다. */
    @Test
    void onlyCitedDocumentsAndValuesThatActuallyNarrowCount() {
        List<DocFacts> docs = List.of(
                doc(1, "예시일보", List.of(), List.of("반도체 수출", "부산"), List.of()),
                doc(2, "예시일보", List.of(), List.of("반도체 수출", "부산", "울산"), List.of()),
                doc(3, "다른일보", List.of(), List.of(), List.of("LG전자")));
        List<FollowUp> chips = ChatFollowUps.suggest(List.of(search("반도체", 30, false, docs)), List.of(ref(1, 1), ref(2, 2)));
        // '반도체 수출'은 검색어와 겹치고, '부산'은 두 근거 모두에 있어 줄이지 못한다. LG전자는 근거가 아니다
        assertThat(texts(chips)).containsExactly("이 중 울산 관련만", "1번 기사 원문 보기");
        assertThat(chips.get(1).url()).isEqualTo("https://news.example.com/1");
    }

    @Test
    void zeroResultsSuggestAWiderPeriodAndNaver() {
        assertThat(texts(ChatFollowUps.suggest(List.of(search("닌텐도", 7, false, List.of())), List.of())))
                .containsExactly("‘닌텐도’ 최근 30일로 넓혀 다시 찾아 줘", "네이버 - 닌텐도");
        assertThat(texts(ChatFollowUps.suggest(List.of(search("닌텐도", 30, false, List.of())), List.of())))
                .containsExactly("‘닌텐도’ 최근 90일로 넓혀 다시 찾아 줘", "네이버 - 닌텐도");
        SearchFacts withSite = new SearchFacts("닌텐도", "korea.kr", List.of(), null, null, null, false, 0, List.of());
        assertThat(texts(ChatFollowUps.suggest(List.of(withSite), List.of())))
                .containsExactly("‘닌텐도’ 조건 없이 다시 찾아 줘", "네이버 - 닌텐도");
    }

    @Test
    void zeroResultsInsideThePreviousListSuggestANewSearch() {
        assertThat(texts(ChatFollowUps.suggest(List.of(search("삼성전자", null, true, List.of())), List.of())))
                .containsExactly("‘삼성전자’ 기사 새로 찾아 줘", "네이버 - 삼성전자");
        SearchFacts bySite = new SearchFacts(null, "이데일리", List.of(), null, null, null, true, 4, List.of());
        assertThat(texts(ChatFollowUps.suggest(List.of(bySite), List.of()))).containsExactly("‘이데일리’ 기사 새로 찾아 줘");
    }

    /** 앞 근거 안에서 거른 결과에는 기간 넓히기를 붙이지 않는다 (넓히면 새 검색이다). */
    @Test
    void narrowedResultsDoNotOfferAWiderPeriod() {
        List<DocFacts> docs = List.of(doc(1, "예시일보", List.of(), List.of(), List.of("삼성전자")),
                doc(2, "이데일리", List.of(), List.of(), List.of()));
        List<FollowUp> chips = ChatFollowUps.suggest(List.of(search("메모리", 7, true, docs)), List.of(ref(1, 1), ref(2, 2)));
        assertThat(texts(chips)).containsExactly("이 중 삼성전자 관련만", "이 중 예시일보 기사만");
    }

    @Test
    void issuesWatchesAndActionsHaveTheirOwnChips() {
        IssueFacts issues = new IssueFacts(3, List.of(new IssueItem(1, "반도체 수출 사상 최대", List.of("반도체", "수출")),
                new IssueItem(2, "가을 축제", List.of("축제"))));
        assertThat(texts(ChatFollowUps.suggest(List.of(issues), List.of())))
                .containsExactly("1번 이슈 언론사 목록", "‘반도체’ 관련 기사 찾아 줘", "최근 7일 이슈로 넓혀 보기");
        assertThat(texts(ChatFollowUps.suggest(List.of(new IssueFacts(3, List.of())), List.of())))
                .containsExactly("최근 7일 이슈로 넓혀 보기");

        WatchFacts watches = new WatchFacts(List.of(new WatchItem("경쟁사", 12), new WatchItem("빈 감시", 0),
                new WatchItem("자사 보도", 3)));
        assertThat(texts(ChatFollowUps.suggest(List.of(watches), List.of())))
                .containsExactly("‘경쟁사’ 감시 문서 보기", "‘자사 보도’ 감시 문서 보기");
        assertThat(texts(ChatFollowUps.suggest(List.of(new ActionFacts(ChatTools.REGISTER_SOURCE)), List.of())))
                .containsExactly("등록 진행 상태 알려 줘");
        // 감시 목록을 본 뒤 감시 문서를 찾았으면 문서 검색 칩
        List<Facts> both = List.of(watches, search("경쟁사", null, false, List.of()));
        assertThat(texts(ChatFollowUps.suggest(both, List.of()))).containsExactly("네이버 - 경쟁사");
        assertThat(ChatFollowUps.suggest(List.of(), List.of())).isEmpty();
    }

    /** 칩은 많아야 3개이고, 눌러 보낸 글이 키워드 바로 검색으로 잘못 가지 않는다 (네이버 칩은 화면이 네이버 결과로 보여 준다). */
    @Test
    void chipsAreAtMostThreeAndNeverMistakenForKeywordSearches() {
        List<DocFacts> docs = new ArrayList<>();
        for (int i = 1; i <= 6; i++) {
            docs.add(doc(i, i % 2 == 0 ? "이데일리" : "예시일보", List.of(i < 4 ? "산업동향" : "정책·규제"),
                    List.of("태그" + (i % 3)), i < 3 ? List.of("삼성전자") : List.of()));
        }
        List<Facts> all = List.of(search("반도체", 3, false, docs));
        List<FollowUp> chips = ChatFollowUps.suggest(all, List.of());
        assertThat(chips).hasSize(3);
        List<String> sent = new ArrayList<>(texts(chips));
        sent.addAll(texts(ChatFollowUps.suggest(List.of(search("닌텐도", 7, false, List.of())), List.of())));
        sent.addAll(texts(ChatFollowUps.suggest(List.of(search("삼성전자", null, true, List.of())), List.of())));
        sent.addAll(texts(ChatFollowUps.suggest(List.of(new IssueFacts(3, List.of(new IssueItem(1, "t", List.of("반도체"))))),
                List.of())));
        sent.addAll(texts(ChatFollowUps.suggest(List.of(new WatchFacts(List.of(new WatchItem("경쟁사", 1)))), List.of())));
        sent.addAll(List.of("감시 목록 보여 줘", "등록 진행 상태 알려 줘"));
        for (String text : sent) {
            if (ChatShortcuts.naverQuery(text).isPresent()) {
                continue;
            }
            assertThat(ChatShortcuts.keyword(text)).as(text).isEmpty();
        }
        assertThat(sent).contains("이 중 삼성전자 관련만", "‘반도체’ 최근 7일로 넓혀 보기");
    }
}
