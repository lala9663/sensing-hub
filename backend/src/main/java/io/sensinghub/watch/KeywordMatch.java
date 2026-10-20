package io.sensinghub.watch;

import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.regex.Pattern;

/**
 * 감시 키워드·제외어 대조 규칙. 워커 watchmatch.py와 같다: 연속 공백을 하나로 줄이고 대소문자를 무시한 뒤, 키워드가 라틴 문자·숫자로
 * 시작하면 바로 앞이, 라틴 문자·숫자로 끝나면 바로 뒤가 라틴 문자·숫자가 아니어야 걸린다 (한글·공백·문장부호·문자열 끝은 경계).
 * "DS"는 "삼성전자 DS부문"·"DS투자증권"·"(DS)"에 걸리고 "applauds"·"MDS테크"·"DSR"에는 안 걸린다. 한글 쪽 끝은 부분 문자열이다
 * (조사가 붙으므로). 섞인 키워드("삼성전자 DS")는 영문 쪽 끝에만 규칙을 쓴다.
 */
public final class KeywordMatch {

    private static final Pattern WS = Pattern.compile("\\s+", Pattern.UNICODE_CHARACTER_CLASS);
    /** 단어 경계로 치지 않는 글자: ASCII 숫자·소문자 라틴 문자와 라틴-1·라틴 확장 A·B의 글자(×·÷ 제외). */
    private static final String LATIN = "0-9a-z\\u00c0-\\u00d6\\u00d8-\\u00f6\\u00f8-\\u024f";
    private static final Pattern LATIN_CHAR = Pattern.compile("[" + LATIN + "]");
    private static final int CACHE_LIMIT = 4096;
    private static final Map<String, Pattern> CACHE = new ConcurrentHashMap<>();

    private KeywordMatch() {
    }

    /** 대조용 정규화: 연속 공백 하나로, 소문자, 앞뒤 공백 제거. */
    public static String normalize(String text) {
        return text == null ? "" : WS.matcher(text).replaceAll(" ").toLowerCase(Locale.ROOT).strip();
    }

    /** 정규화한 text({@link #normalize})에 keyword가 영문 단어 경계 규칙대로 들어 있는지. 빈 키워드는 false. */
    public static boolean contains(String normalizedText, String keyword) {
        String k = normalize(keyword);
        if (k.isEmpty() || normalizedText == null) {
            return false;
        }
        return pattern(k).matcher(normalizedText).find();
    }

    private static Pattern pattern(String k) {
        Pattern cached = CACHE.get(k);
        if (cached != null) {
            return cached;
        }
        String left = isLatin(k.charAt(0)) ? "(?<![" + LATIN + "])" : "";
        String right = isLatin(k.charAt(k.length() - 1)) ? "(?![" + LATIN + "])" : "";
        Pattern p = Pattern.compile(left + Pattern.quote(k) + right);
        if (CACHE.size() < CACHE_LIMIT) {
            CACHE.put(k, p);
        }
        return p;
    }

    private static boolean isLatin(char c) {
        return LATIN_CHAR.matcher(String.valueOf(c)).matches();
    }
}
