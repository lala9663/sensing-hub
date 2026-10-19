package io.sensinghub.chat;

import java.util.Locale;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 사용자가 채팅에 붙여 넣은 API 키처럼 보이는 값을 저장·전송 전에 가린다.
 * 대화는 DB에 영구 저장되고 외부 LLM에도 보내지므로, 1단계는 키를 채팅으로 받지 않고(관리자 'API 키' 화면으로 안내)
 * 실수로 붙여 넣은 값도 남기지 않는다. 주소(://, www., 점이 든 낱말)는 건드리지 않는다.
 * <ul>
 *   <li>알려진 접두어: sk-ant-, sk-, AIza, ghp_, github_pat_, xoxb- 등 + 16자 이상</li>
 *   <li>16진수 32자 이상 (DART 인증키는 40자)</li>
 *   <li>id:secret 형식 (네이버 client_id:client_secret)</li>
 *   <li>그 밖에 영문·숫자가 섞인 32자 이상의 기호 없는 덩어리 (공공데이터포털 서비스키 등)</li>
 * </ul>
 */
final class SecretMasker {

    static final String MASK = "[API 키로 보이는 값 — 저장하지 않음]";

    private static final Pattern TOKEN = Pattern.compile("\\S+");
    private static final Pattern RUN = Pattern.compile("[A-Za-z0-9%+/=_\\-:.]+");
    private static final Pattern PREFIXED = Pattern.compile(
            "(?i)(sk-ant-|sk-|AIza|ghp_|gho_|github_pat_|xox[abp]-)[A-Za-z0-9_\\-]{16,}");
    private static final Pattern HEX = Pattern.compile("[0-9a-fA-F]{32,}");
    private static final Pattern ID_SECRET = Pattern.compile("[A-Za-z0-9_\\-]{16,}:[A-Za-z0-9_\\-]{8,}");
    private static final Pattern GENERIC = Pattern.compile("[A-Za-z0-9%+/=_\\-]{32,}");

    record Result(String text, int masked) {
    }

    private SecretMasker() {
    }

    static Result mask(String text) {
        if (text == null || text.isEmpty()) {
            return new Result(text, 0);
        }
        StringBuilder out = new StringBuilder();
        int masked = 0;
        int last = 0;
        Matcher tokens = TOKEN.matcher(text);
        while (tokens.find()) {
            out.append(text, last, tokens.start());
            String token = tokens.group();
            String lower = token.toLowerCase(Locale.ROOT);
            if (lower.contains("://") || lower.startsWith("www.")) {
                out.append(token);
            } else {
                int at = 0;
                Matcher runs = RUN.matcher(token);
                while (runs.find()) {
                    out.append(token, at, runs.start());
                    String run = runs.group();
                    String core = run.replaceAll("[.:]+$", "");
                    if (isSecret(core)) {
                        out.append(MASK).append(run.substring(core.length()));
                        masked++;
                    } else {
                        out.append(run);
                    }
                    at = runs.end();
                }
                out.append(token.substring(at));
            }
            last = tokens.end();
        }
        out.append(text.substring(last));
        return new Result(out.toString(), masked);
    }

    static boolean isSecret(String s) {
        if (s.length() < 16 || s.contains(".")) {
            return false;
        }
        if (PREFIXED.matcher(s).matches() || HEX.matcher(s).matches() || ID_SECRET.matcher(s).matches()) {
            return true;
        }
        return GENERIC.matcher(s).matches() && s.chars().anyMatch(Character::isDigit)
                && s.chars().anyMatch(Character::isLetter);
    }
}
