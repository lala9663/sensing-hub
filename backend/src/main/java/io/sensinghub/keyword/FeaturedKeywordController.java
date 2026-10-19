package io.sensinghub.keyword;

import java.util.List;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import io.sensinghub.auth.CurrentUser;
import io.sensinghub.keyword.FeaturedKeywordService.FeaturedKeyword;
import io.sensinghub.keyword.FeaturedKeywordService.KeywordList;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotNull;

/**
 * 주요 키워드 (네이버 뉴스 탭·챗봇의 빠른 검색 칩). 읽기는 VIEWER 이상, 바꾸기는 ADMIN.
 * PUT은 목록 전체를 보낸 순서대로 바꾼다: {"items":[{"label":"DS","query":"삼성전자 DS"}, ...]} (query를 비우면 label과 같다).
 */
@RestController
@RequestMapping("/api/v1/featured-keywords")
public class FeaturedKeywordController {

    private final FeaturedKeywordService service;

    public FeaturedKeywordController(FeaturedKeywordService service) {
        this.service = service;
    }

    @GetMapping
    public KeywordList list(CurrentUser user) {
        return service.list(user);
    }

    public record ReplaceRequest(@NotNull List<FeaturedKeyword> items) {
    }

    /** items가 없으면 400 (빈 본문으로 목록이 지워지지 않게). 빈 배열은 "칩 없음"이다. */
    @PutMapping
    public KeywordList replace(CurrentUser user, @Valid @RequestBody ReplaceRequest body) {
        return service.replace(user, body.items());
    }
}
