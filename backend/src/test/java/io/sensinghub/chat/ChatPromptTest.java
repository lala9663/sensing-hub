package io.sensinghub.chat;

import static org.assertj.core.api.Assertions.assertThat;

import java.time.ZonedDateTime;

import org.junit.jupiter.api.Test;

import io.sensinghub.auth.CurrentUser;
import io.sensinghub.auth.Role;

class ChatPromptTest {

    @Test
    void systemPromptDefinesRelationTopicsByTheAdminList() {
        CurrentUser user = new CurrentUser(3, "viewer", "일반 사용자", Role.VIEWER);
        String summary = "자사: 삼성전자 / 경쟁사: (아직 없음) / 고객사: (아직 없음) / 협력사: (아직 없음)";
        String prompt = ChatPrompt.system(user, ZonedDateTime.parse("2026-10-20T09:00:00+09:00[Asia/Seoul]"), summary);
        assertThat(prompt).contains("지금 목록 — " + summary)
                .contains("자사, 경쟁사, 고객사, 협력사, 산업동향")
                .contains("관리자가 정한 회사 관계 목록")
                .contains("단정하지 않는다");
        assertThat(ChatPrompt.system(user, ZonedDateTime.now(), null)).contains("지금 목록 — (알 수 없음)");
    }
}
