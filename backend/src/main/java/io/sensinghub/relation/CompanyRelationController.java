package io.sensinghub.relation;

import java.util.List;

import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;

import io.sensinghub.auth.CurrentUser;
import io.sensinghub.relation.CompanyRelationService.HistoryEntry;
import io.sensinghub.relation.CompanyRelationService.RelationInput;
import io.sensinghub.relation.CompanyRelationService.RelationList;
import io.sensinghub.relation.CompanyRelationService.RelationView;

/**
 * 회사 관계 목록 (문서 분류의 자사·경쟁사·고객사·협력사 라벨). 읽기는 VIEWER 이상, 바꾸기와 변경 기록은 ADMIN.
 * <pre>
 * GET    /api/v1/company-relations            {items[{id, name, aliases[], exclude[], relation, label, note, updatedAt, updatedBy}], relations{코드: 이름}}
 * POST   /api/v1/company-relations            {name, aliases[], exclude[], relation: OWN|COMPETITOR|CUSTOMER|PARTNER, note}
 * PUT    /api/v1/company-relations/{id}       같은 본문 (전체 교체)
 * DELETE /api/v1/company-relations/{id}
 * GET    /api/v1/company-relations/history    최근 변경 30건 (감사 로그)
 * </pre>
 */
@RestController
@RequestMapping("/api/v1/company-relations")
public class CompanyRelationController {

    private final CompanyRelationService service;

    public CompanyRelationController(CompanyRelationService service) {
        this.service = service;
    }

    @GetMapping
    public RelationList list(CurrentUser user) {
        return service.list(user);
    }

    @PostMapping
    @ResponseStatus(HttpStatus.CREATED)
    public RelationView create(CurrentUser user, @RequestBody RelationInput body) {
        return service.create(user, body);
    }

    @PutMapping("/{id}")
    public RelationView update(CurrentUser user, @PathVariable long id, @RequestBody RelationInput body) {
        return service.update(user, id, body);
    }

    @DeleteMapping("/{id}")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    public void delete(CurrentUser user, @PathVariable long id) {
        service.delete(user, id);
    }

    @GetMapping("/history")
    public List<HistoryEntry> history(CurrentUser user) {
        return service.history(user);
    }
}
