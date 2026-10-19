package io.sensinghub.chat;

import static org.assertj.core.api.Assertions.assertThat;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Optional;

import org.junit.jupiter.api.Test;

/**
 * 키워드만·네이버 형식 판별. 사례는 chat-shortcuts-cases.tsv에 두고 화면(static/chat.js)도 같은 파일로 확인한다
 * (scripts/dev/check-chat-shortcuts.mjs).
 */
class ChatShortcutsTest {

    private record Case(String kind, String input, String expected) {
    }

    private static List<Case> cases() throws IOException {
        try (InputStream in = ChatShortcutsTest.class.getResourceAsStream("/chat-shortcuts-cases.tsv")) {
            List<Case> out = new ArrayList<>();
            for (String line : new String(in.readAllBytes(), StandardCharsets.UTF_8).split("\r?\n")) {
                if (line.isEmpty() || line.startsWith("#")) {
                    continue;
                }
                String[] f = line.split("\t", -1);
                out.add(new Case(f[0], f[1], f[2]));
            }
            return out;
        }
    }

    @Test
    void casesMatch() throws IOException {
        List<Case> all = cases();
        assertThat(all).hasSizeGreaterThan(40);
        for (Case c : all) {
            Optional<String> got = "naver".equals(c.kind()) ? ChatShortcuts.naverQuery(c.input()) : ChatShortcuts.keyword(c.input());
            String shown = got.map(q -> q.isEmpty() ? "EMPTY" : q).orElse("NONE");
            assertThat(shown).as(c.kind() + " [" + c.input() + "]").isEqualTo(c.expected());
        }
    }

    /** 네이버 형식이면 키워드 경로로 가지 않는다 (서버는 네이버 형식을 먼저 거절한다). */
    @Test
    void naverFormIsCheckedBeforeKeyword() {
        assertThat(ChatShortcuts.naverQuery("네이버 닌텐도")).contains("닌텐도");
        assertThat(ChatShortcuts.keyword("네이버 닌텐도")).contains("네이버 닌텐도");
        assertThat(ChatShortcuts.naverQuery("네이버")).isEmpty();
    }
}
