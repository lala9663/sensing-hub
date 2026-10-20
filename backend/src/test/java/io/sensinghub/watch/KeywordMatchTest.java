package io.sensinghub.watch;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

/** 감시 키워드·제외어 대조 (워커 watchmatch.py와 같은 영문 단어 경계 규칙). DB 없이 돈다. */
class KeywordMatchTest {

    private static boolean hit(String text, String keyword) {
        return KeywordMatch.contains(KeywordMatch.normalize(text), keyword);
    }

    @Test
    void shortLatinKeywordNeedsWordBoundaries() {
        assertThat(hit("삼성전자 DS부문 실적", "DS")).isTrue();      // 뒤가 한글
        assertThat(hit("DS투자증권 보고서", "DS")).isTrue();
        assertThat(hit("사업부(DS) 개편", "DS")).isTrue();           // 문장부호
        assertThat(hit("ds", "DS")).isTrue();                        // 문자열 끝, 대소문자 무시
        assertThat(hit("Yoon applauds chip makers", "DS")).isFalse();
        assertThat(hit("MDS테크 수주", "DS")).isFalse();             // 앞이 라틴 문자
        assertThat(hit("GADS 도입", "DS")).isFalse();
        assertThat(hit("DSR 규제", "DS")).isFalse();                 // 뒤가 라틴 문자
        assertThat(hit("DS2 출시", "DS")).isFalse();                 // 숫자도 단어 글자
    }

    @Test
    void latinEndsOnlyAndHangulStaysSubstring() {
        assertThat(hit("DSR 규제 강화", "DSR")).isTrue();
        assertThat(hit("스트레스DSR 3단계", "DSR")).isTrue();       // 앞이 한글
        assertThat(hit("DSRS 표준", "DSR")).isFalse();
        assertThat(hit("반도체가 살아난다", "반도체")).isTrue();     // 조사가 붙어도
        assertThat(hit("SK하이닉스 HBM", "하이닉스")).isTrue();
        assertThat(hit("삼성전자 DS부문", "삼성전자 DS")).isTrue();  // 섞인 키워드: 영문 쪽 끝만 본다
        assertThat(hit("삼성전자 DSR", "삼성전자 DS")).isFalse();
        assertThat(hit("삼성전자  ds", "삼성전자 DS")).isTrue();     // 연속 공백
        assertThat(hit("Café 개업", "café")).isTrue();
        assertThat(hit("Cafés", "café")).isFalse();                 // 라틴 확장 글자도 단어 글자
    }

    @Test
    void emptyKeywordOrTextNeverMatches() {
        assertThat(hit("아무 글", " ")).isFalse();
        assertThat(hit(null, "DS")).isFalse();
        assertThat(KeywordMatch.contains(null, "DS")).isFalse();
    }
}
