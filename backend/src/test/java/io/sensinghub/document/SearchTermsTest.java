package io.sensinghub.document;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

/** 검색어 → 전문 검색 낱말·tsquery (워커 정규식 분석기와 같은 규칙). DB 없이 돈다. */
class SearchTermsTest {

    @Test
    void splitsOnPunctuationLowercasesAndStripsParticles() {
        assertThat(SearchTerms.terms("반도체가 수출을")).containsExactly("반도체", "수출");
        assertThat(SearchTerms.terms("  HBM3E,  삼성전자의…공급 ")).containsExactly("hbm3e", "삼성전자", "공급");
        assertThat(SearchTerms.terms("정부에서는 발표했다")).containsExactly("정부", "발표");
        assertThat(SearchTerms.terms("반도체 반도체가")).containsExactly("반도체");     // 중복 제거
        assertThat(SearchTerms.terms("ＡＩ 칩")).containsExactly("ai", "칩");         // 전각 문자(NFKC)
    }

    @Test
    void keepsShortStemsAndNonHangulEndings() {
        assertThat(SearchTerms.stripSuffix("국가")).isEqualTo("국가");     // 국가 → 국 으로 훼손하지 않는다
        assertThat(SearchTerms.stripSuffix("결과")).isEqualTo("결과");
        assertThat(SearchTerms.stripSuffix("국가가")).isEqualTo("국가");
        assertThat(SearchTerms.stripSuffix("lg")).isEqualTo("lg");
    }

    @Test
    void onlyPunctuationGivesNoTerms() {
        assertThat(SearchTerms.terms("!!! ... ???")).isEmpty();
        assertThat(SearchTerms.terms(null)).isEmpty();
    }

    @Test
    void buildsQuotedPrefixQueriesAndEscapesSpecialCharacters() {
        assertThat(SearchTerms.prefixQuery("반도체")).isEqualTo("'반도체':*");
        // 낱말에는 글자·숫자만 남지만, 직접 부를 때도 tsquery 문법이 깨지지 않게 감싼다
        assertThat(SearchTerms.prefixQuery("a'b\\c")).isEqualTo("'a''b\\\\c':*");
        assertThat(SearchTerms.terms("a & b | !c:*")).containsExactly("a", "b", "c");
        assertThat(SearchTerms.likePattern("50%_off")).isEqualTo("%50\\%\\_off%");
    }
}
