package io.sensinghub.chat;

import static org.assertj.core.api.Assertions.assertThat;

import java.time.OffsetDateTime;

import org.junit.jupiter.api.Test;

import io.sensinghub.chat.ChatTools.Refs;

/** 채팅에 붙여 넣은 키처럼 보이는 값 가리기, 답의 [n] 근거 고르기. */
class SecretMaskerTest {

    private static String masked(String text) {
        return SecretMasker.mask(text).text();
    }

    @Test
    void masksKeyLikeValues() {
        assertThat(masked("DART 키는 0123456789abcdef0123456789abcdef01234567 이에요"))
                .isEqualTo("DART 키는 " + SecretMasker.MASK + " 이에요");
        assertThat(masked("키: sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv.")).isEqualTo("키: " + SecretMasker.MASK + ".");
        // 지어낸 값이지만 키 형식이라 저장소의 비밀 탐지에 걸리지 않게 나눠 쓴다
        assertThat(masked("AIza" + "SyA1b2C3d4E5f6G7h8I9j0KlMnOpQrStUvW 넣어줘")).startsWith(SecretMasker.MASK);
        assertThat(masked("네이버 abcdEFGH12345678xyzw:Q1w2E3r4T5 등록")).isEqualTo("네이버 " + SecretMasker.MASK + " 등록");
        // 공공데이터포털 서비스키 (인코딩된 형태)
        assertThat(masked("서비스키는 Xy7%2BkLm9Qp3Rt5Uv8Wz1Ab4Cd6Ef0Gh2Ij%3D%3D 입니다"))
                .isEqualTo("서비스키는 " + SecretMasker.MASK + " 입니다");
        // 한글에 붙어 있어도
        assertThat(masked("키는0123456789abcdef0123456789abcdef01234567")).isEqualTo("키는" + SecretMasker.MASK);
        assertThat(SecretMasker.mask("a 0123456789abcdef0123456789abcdef b 0123456789abcdef0123456789abcdef").masked())
                .isEqualTo(2);
    }

    @Test
    void leavesUrlsWordsAndNumbersAlone() {
        for (String text : new String[] {
                "https://www.yna.co.kr/view/AKR20261019012345678?section=news&key=0123456789abcdef0123456789abcdef 등록해 줘",
                "www.example.com/path/0123456789abcdef0123456789abcdef",
                "dart.fss.or.kr 수집해 줘",
                "최근 3일 반도체 기사 찾아 줘",
                "사업자번호 123-45-67890, 2026-10-19T09:00:00 이후",
                "electroencephalographically 아주 긴 영어 낱말",
                "registration_id 12345 상태"}) {
            assertThat(masked(text)).as(text).isEqualTo(text);
        }
    }

    @Test
    void citationsFollowRefsUsedInTheAnswerElseAllRefs() {
        Refs refs = new Refs();
        OffsetDateTime t = OffsetDateTime.now();
        refs.document(10, "기사 A", "https://a.example/1", "A일보", t);
        refs.document(11, "기사 B", "https://b.example/1", "B일보", t);
        refs.issue(5, "이슈", "언론사 3곳 · 기사 4건", t);
        assertThat(refs.document(10, "기사 A", "https://a.example/1", "A일보", t)).isEqualTo(1);   // 같은 문서는 같은 번호

        assertThat(ChatService.citations("B가 먼저 [2], 이슈 [3, 1], 없는 번호 [9]", refs))
                .extracting(ChatTools.Ref::no).containsExactly(2, 3, 1);
        assertThat(ChatService.citations("번호 없이 답함", refs)).extracting(ChatTools.Ref::no).containsExactly(1, 2, 3);
        assertThat(ChatService.citations("[1]", new Refs())).isEmpty();
        assertThat(refs.get(3).url()).isEqualTo("#/issues/5");
        assertThat(refs.get(3).kind()).isEqualTo("ISSUE");
    }
}
