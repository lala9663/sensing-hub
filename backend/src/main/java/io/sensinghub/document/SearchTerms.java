package io.sensinghub.document;

import java.text.Normalizer;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;

/**
 * 검색어를 전문 검색 낱말로 바꾼다 (M5 기반). 워커가 색인할 때 쓰는 정규식 분석기(textindex.py)와 같은 규칙이다:
 * 글자·숫자가 아닌 문자로 나누고, 라틴 문자는 소문자로, 끝의 조사·어미를 하나 뗀다(뗀 나머지가 2글자 이상일 때만).
 * 그래서 "반도체가"는 "반도체"로 바뀌어 색인의 '반도체'에 맞는다. Java에는 형태소 분석기가 없으므로 질의 쪽은
 * 띄어쓰기 단위로만 나누고, 색인 쪽(Kiwi)이 붙여 쓴 복합어도 함께 넣어 둔다.
 * <p>각 낱말은 접두 일치(:*)로 찾는다. 낱말은 글자·숫자로만 이뤄지므로 tsquery 특수문자(&|!():*'\)가 들어갈 수 없지만,
 * 그래도 작은따옴표로 감싸고 안의 따옴표·역슬래시를 이스케이프한다.
 */
public final class SearchTerms {

    /** 워커 textindex.SUFFIXES와 같은 목록. 긴 것부터 본다. */
    static final List<String> SUFFIXES = List.of(
            "에서는", "에게서", "으로는", "으로써", "으로서", "이라는", "이라고", "에서도", "까지는", "부터는",
            "에서", "에게", "으로", "까지", "부터", "한테", "처럼", "보다", "마저", "조차", "이나", "이며", "이다",
            "에는", "에도", "와의", "과의", "로서", "로써", "라는", "라고",
            "한다", "했다", "된다", "됐다", "하는", "했던", "하며", "하고", "해야",
            "은", "는", "이", "가", "을", "를", "의", "에", "로", "과", "와", "도", "만").stream()
            .sorted(Comparator.comparingInt(String::length).reversed()).toList();

    private static final int MAX_TERMS = 10;

    private SearchTerms() {
    }

    /** 검색어 → 낱말 목록 (중복 제거, 순서 유지, 최대 10개). 글자·숫자가 없으면 빈 목록. */
    public static List<String> terms(String query) {
        if (query == null) {
            return List.of();
        }
        String text = Normalizer.normalize(query, Normalizer.Form.NFKC).toLowerCase(Locale.ROOT);
        Set<String> out = new LinkedHashSet<>();
        for (String raw : text.split("[^\\p{L}\\p{N}]+")) {
            if (raw.isEmpty()) {
                continue;
            }
            out.add(stripSuffix(raw));
            if (out.size() >= MAX_TERMS) {
                break;
            }
        }
        return new ArrayList<>(out);
    }

    /** 끝의 조사·어미 하나. 한글로 끝나지 않거나 뗀 나머지가 2글자 미만이면 그대로. */
    static String stripSuffix(String token) {
        if (token.isEmpty() || !isHangul(token.charAt(token.length() - 1))) {
            return token;
        }
        for (String s : SUFFIXES) {
            if (token.endsWith(s) && token.length() - s.length() >= 2) {
                return token.substring(0, token.length() - s.length());
            }
        }
        return token;
    }

    private static boolean isHangul(char c) {
        return c >= '가' && c <= '힣';
    }

    /** 낱말 하나의 접두 일치 tsquery: '반도체':* */
    public static String prefixQuery(String term) {
        return "'" + term.replace("\\", "\\\\").replace("'", "''") + "':*";
    }

    /** ILIKE 패턴: %낱말% (%, _, \ 이스케이프). */
    public static String likePattern(String term) {
        return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%";
    }
}
