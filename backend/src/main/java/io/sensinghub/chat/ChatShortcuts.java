package io.sensinghub.chat;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Optional;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 챗봇 입력의 두 가지 바로 가기 판별. 화면(static/chat.js의 Chat.shortcut)도 같은 규칙을 쓴다 — 바꾸면 둘 다 바꾼다.
 * <ul>
 *   <li><b>키워드만</b> ({@link #keyword}): 물음표·문장부호·요청 말(알려·찾아·보여·해줘·요약 …)·질문 말(뭐·어디 …)·때 말(오늘·최근 …)·
 *       허브 기능 말(이슈·감시·등록 …) 없이 짧은 낱말 1~4개, 40자 이하. "반도체", "삼성전자 DS", "메타넷 글로벌".
 *       LLM을 거치지 않고 수집 기사에서 바로 찾는다. 끝의 '기사'·'뉴스'·'관련' 같은 낱말은 검색어에서 뺀다.</li>
 *   <li><b>네이버 형식</b> ({@link #naverQuery}): 맨 앞 '네이버'(또는 '네이버 뉴스') + 구분자(-, :, 대시) + 검색어.
 *       구분자 없이 띄어 쓴 "네이버 닌텐도"는 뒤가 키워드만일 때만 (그래야 "네이버 관련 기사 찾아 줘" 같은 질문을 가로채지 않는다).
 *       네이버 결과는 표시 전용이라 화면이 직접 불러 보여 주고, 대화로 저장하거나 LLM에 보내지 않는다.</li>
 * </ul>
 */
final class ChatShortcuts {

    static final int MAX_KEYWORD_CHARS = 40;
    static final int MAX_KEYWORD_WORDS = 4;

    /** 낱말: 글자·숫자로 시작하고 끝난다. 가운데에는 & · . / + _ - 를 쓸 수 있다 (S&P, AI·반도체, LG-에너지). */
    private static final Pattern WORD = Pattern.compile("[\\p{L}\\p{N}](?:[\\p{L}\\p{N}&·./+_-]*[\\p{L}\\p{N}])?");
    private static final Pattern JAMO_ONLY = Pattern.compile("[ㄱ-ㅎㅏ-ㅣ]+");
    /** "3일", "10개", "2번", "5시간" — 기간·개수·번호는 질문이다 (LLM이 days·limit으로 바꾼다). */
    private static final Pattern COUNT = Pattern.compile("\\d+(?:일|주|주일|주간|개|건|번|번째|시간|분|달|개월|월|년)");
    private static final Pattern NAVER_SEPARATED = Pattern.compile("^네이버(?:\\s*뉴스)?\\s*[-:：–—]\\s*(.*)$");
    private static final Pattern NAVER_SPACED = Pattern.compile("^네이버\\s+(.+)$");

    /** 이 말이 들어 있으면 요청·질문이다 (띄어쓰기 없이 비교). 낱말 안에 흔히 든 말(정리해고·추가경정예산·설명회·해주)은 넣지 않는다. */
    static final List<String> REQUEST_STEMS = List.of(
            "알려", "찾아", "찾기", "검색해", "보여", "해줘", "주세요", "줘", "요약", "설명해", "궁금",
            "무엇", "뭐", "뭔", "어떻", "어떤", "어때", "어디", "언제", "누구", "누가", "얼마", "있어", "있나", "있니", "있을까",
            "없어", "했어", "했나", "됐어", "할까", "해봐", "해라", "만들어", "추가해", "삭제해", "지워", "비교해",
            "분석해", "알아봐", "가르쳐", "부탁");

    /** 이 낱말이 하나라도 있으면 키워드만이 아니다 (질문·인사·때·허브 기능·가리키는 말). 소문자로 비교. */
    static final Set<String> STOP_WORDS = Set.of(
            "왜", "뭘", "뭔가", "이슈", "이슈들", "감시", "등록", "진행", "상태", "목록", "요청", "다이제스트", "구독", "챗봇", "키워드",
            "도움말", "도움", "사용법", "안녕", "안녕하세요", "하이", "헬로", "고마워", "고마워요", "감사", "감사합니다", "땡큐", "수고",
            "응", "네", "예", "아니", "아니요", "그래", "좋아", "오케이", "ok", "okay", "yes", "no", "hi", "hello", "hey", "help",
            "thanks", "test", "테스트", "what", "why", "how", "who", "when", "where", "show", "find", "search", "list",
            "오늘", "어제", "그제", "내일", "최근", "요즘", "이번", "지난", "이번주", "지난주", "이번달", "지난달", "금주", "올해",
            "작년", "지금", "주간", "일간", "더", "다시", "계속", "그거", "이거", "저거", "그중", "위", "아래", "다음", "이전");

    /** 검색어에서 빼는 말 ("반도체 관련 기사" → "반도체"). 이것만 있으면 키워드가 아니다. */
    static final Set<String> FILLER_WORDS = Set.of("관련", "기사", "기사들", "뉴스", "소식", "보도", "동향", "자료", "문서",
            "news", "article", "articles");

    private ChatShortcuts() {
    }

    static String flat(String text) {
        return text == null ? "" : text.replaceAll("\\s+", " ").strip();
    }

    /** 키워드만이면 찾을 검색어 (채움 말을 뺀 것), 아니면 비어 있다. */
    static Optional<String> keyword(String text) {
        String t = flat(text);
        if (t.isEmpty() || t.length() > MAX_KEYWORD_CHARS) {
            return Optional.empty();
        }
        String[] words = t.split(" ");
        if (words.length > MAX_KEYWORD_WORDS) {
            return Optional.empty();
        }
        String compact = t.replace(" ", "").toLowerCase(Locale.ROOT);
        if (REQUEST_STEMS.stream().anyMatch(compact::contains)) {
            return Optional.empty();
        }
        List<String> query = new ArrayList<>();
        for (String w : words) {
            String lower = w.toLowerCase(Locale.ROOT);
            if (!WORD.matcher(w).matches() || JAMO_ONLY.matcher(w).matches() || COUNT.matcher(w).matches()
                    || STOP_WORDS.contains(lower)) {
                return Optional.empty();
            }
            if (!FILLER_WORDS.contains(lower)) {
                query.add(w);
            }
        }
        return query.isEmpty() ? Optional.empty() : Optional.of(String.join(" ", query));
    }

    /** 네이버 형식이면 네이버 검색어 (구분자 뒤가 비었으면 빈 문자열), 아니면 비어 있다. */
    static Optional<String> naverQuery(String text) {
        String t = flat(text);
        Matcher separated = NAVER_SEPARATED.matcher(t);
        if (separated.matches()) {
            return Optional.of(separated.group(1).strip());
        }
        Matcher spaced = NAVER_SPACED.matcher(t);
        if (spaced.matches()) {
            return keyword(spaced.group(1));
        }
        return Optional.empty();
    }
}
