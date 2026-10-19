"""전문 검색 색인(textindex)과 이슈 묶기(issues)의 단위 테스트. DB 없이 돈다.

Kiwi(kiwipiepy) 경로는 설치돼 있을 때만, 정규식 경로는 항상 돈다. 이슈 묶기 테스트는 두 분석기 모두로 돈다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sensing_worker import issues, textindex
from sensing_worker.issues import Candidate, MemoryClusterer, Rule

KST = timezone(timedelta(hours=9))
T0 = datetime(2026, 10, 18, 9, 0, tzinfo=KST)


@pytest.fixture(params=["kiwi", "regex"])
def analyzer(request, monkeypatch):
    """두 분석기로 같은 테스트를 돈다. kiwi는 설치돼 있지 않으면 건너뛴다."""
    if request.param == "kiwi":
        pytest.importorskip("kiwipiepy")
        assert textindex._kiwi_instance() is not None
    else:
        monkeypatch.setattr(textindex, "_kiwi_instance", lambda: None)
    return request.param


# ---------------------------------------------------------------------- 정규식 분석기

def test_regex_strips_particles_and_endings_but_keeps_short_stems():
    assert textindex.strip_suffix("반도체가") == "반도체"
    assert textindex.strip_suffix("정부에서는") == "정부"
    assert textindex.strip_suffix("발표했다") == "발표"
    assert textindex.strip_suffix("서울시의") == "서울시"
    # 뗀 나머지가 2글자 미만이면 떼지 않는다: 국가→국, 결과→결, 회의→회 같은 훼손을 막는다
    assert textindex.strip_suffix("국가") == "국가"
    assert textindex.strip_suffix("결과") == "결과"
    assert textindex.strip_suffix("회의") == "회의"
    assert textindex.strip_suffix("국가가") == "국가"
    assert textindex.strip_suffix("HBM") == "HBM"   # 한글로 끝나지 않으면 그대로


def test_regex_terms_lowercase_latin_numbers_and_short_tokens():
    terms = textindex.regex_terms("삼성전자가 3분기 HBM3E를 공급한다… 2026 AI 및 칩")
    assert terms == ["삼성전자", "3분기", "hbm3e", "공급", "ai"]
    assert "2026" not in terms                        # 숫자만인 덩어리는 기본 낱말에서 뺀다
    assert "2026" in textindex.regex_index_terms("2026 예산")   # 색인에는 넣는다 (연도 검색)
    assert "및" not in terms                           # 1글자는 버린다
    assert textindex.regex_terms("&middot; 문화&middot;체육") == ["문화", "체육"]   # HTML 엔티티를 푼다


# ---------------------------------------------------------------------- Kiwi 분석기

def test_kiwi_keeps_nouns_foreign_words_and_attached_numbers():
    pytest.importorskip("kiwipiepy")
    terms = textindex.kiwi_terms("삼성전자가 3분기 영업이익 10조원을 기록했다. AI 반도체 HBM3E 공급 확대")
    for t in ("삼성전자", "3분기", "영업", "이익", "10조원", "기록", "ai", "반도체", "hbm3e", "공급", "확대"):
        assert t in terms
    assert "가" not in terms and "을" not in terms     # 조사는 버린다
    assert "분기" not in terms                         # 숫자에 붙은 단위는 따로 세지 않는다
    index = textindex.index_terms("삼성전자가 3분기 영업이익 10조원을 기록했다")
    assert "영업이익" in index and "분기" in index       # 띄어 쓰지 않은 복합어와 단위 명사는 색인에 더한다
    assert "삼성전자가" not in index


def test_kiwi_drops_one_letter_nouns_and_bare_numbers():
    pytest.importorskip("kiwipiepy")
    terms = textindex.kiwi_terms("李대통령 말 한마디에 25% 급등")
    assert "말" not in terms and "25" not in terms
    assert "대통령" in terms and "급등" in terms


def test_kiwi_unknown_proper_noun_loses_its_particle():
    pytest.importorskip("kiwipiepy")
    index = textindex.index_terms("LG에너지솔루션, 창신메모리의 추격")
    assert "lg에너지솔루션" in index and "에너지솔루션" in index and "창신메모리" in index


def test_index_terms_cover_backend_query_words(analyzer):
    """백엔드는 질의를 띄어쓰기 단위로 나눠 조사만 뗀다. 그 낱말이 색인에 있어야 접두 일치로 찾는다."""
    index = textindex.index_terms("반도체가 영업이익을 끌어올렸다")
    assert "반도체" in index and "영업이익" in index


# ---------------------------------------------------------------------- 조각 나누기

def test_chunks_title_first_then_paragraphs_up_to_size():
    body = "\n\n".join(f"{i}번 문단입니다. " * 10 for i in range(5))   # 문단 약 100자 × 5
    chunks = textindex.chunk_document("  제목   입니다 ", body, size=250)
    assert chunks[0] == "제목 입니다"
    assert all(len(c) <= 250 for c in chunks[1:])
    assert len(chunks) == 1 + 3                                         # 100+100 / 100+100 / 100
    assert "0번 문단" in chunks[1] and "4번 문단" in chunks[-1]


def test_chunks_split_long_paragraph_on_sentences_and_hard_cut():
    long_para = "가" * 50 + ". " + "나" * 1900
    chunks = textindex.chunk_body(long_para, size=800)
    assert chunks[0].startswith("가" * 50)
    assert all(len(c) <= 800 for c in chunks)
    assert "".join(c.replace("\n", "").replace(". ", ".") for c in chunks).count("나") == 1900


def test_chunks_are_capped_and_press_has_title_only():
    body = "\n\n".join("문단 " * 200 for _ in range(50))
    chunks = textindex.chunk_document("제목", body)
    assert len(chunks) == textindex.MAX_CHUNKS and chunks[0] == "제목"
    assert textindex.chunk_document("언론 기사 제목", None) == ["언론 기사 제목"]
    assert textindex.chunk_document(None, None) == []


# ---------------------------------------------------------------------- 이슈 묶기 규칙

def test_clean_title_removes_tags_and_outlet_tails():
    assert issues.clean_title("[단독] 할리스 개인정보 유출(종합) - 경제ㅣ한국일보") == "할리스 개인정보 유출"
    assert issues.clean_title("【속보】 코스피 급락 | 연합뉴스") == "코스피 급락"
    assert issues.clean_title("[AI는 지금] 메타, 약관 수정") == "메타, 약관 수정"


def test_title_terms_skip_stop_terms(analyzer):
    terms = issues.title_terms("[속보] 정부, 오늘 반도체 지원 발표")
    assert "속보" not in terms and "정부" not in terms and "오늘" not in terms and "발표" not in terms
    assert "반도체" in terms and "지원" in terms


def test_rule_needs_two_shared_terms_and_strict_score_for_pairs():
    rule = Rule(threshold=0.4, min_shared=2, pair_threshold=0.66)
    assert not rule.accepts(1, 1.0)            # 1개만 겹치면 안 된다
    assert not rule.accepts(2, 0.5)            # 2개 겹침은 짧은 제목(3개 중 2개 이상)일 때만
    assert rule.accepts(2, 2 / 3)
    assert rule.accepts(3, 0.43)
    assert not rule.accepts(3, 0.38)


def test_best_match_uses_overlap_coefficient_and_prefers_higher_score():
    doc = ["야스쿠니", "공물", "봉납", "다카이치", "외교부"]
    a = Candidate(1, ["야스쿠니", "공물", "봉납", "다카이치", "총리", "신사", "유감"], T0)
    b = Candidate(2, ["다카이치", "총리", "취임", "내각", "지지율"], T0)
    m = issues.best_match(doc, [b, a], Rule())
    assert m is not None and m.issue_id == 1 and m.shared == 4 and m.score == pytest.approx(4 / 5)
    assert issues.best_match(["다카이치", "외교부"], [b], Rule()) is None


def test_top_terms_by_frequency_then_first_seen():
    assert issues.top_terms([["a", "b", "c"], ["b", "c", "d"], ["c", "e"]], k=3) == ["c", "b", "a"]


def test_representative_is_most_central_then_earliest():
    m1 = issues.Member(1, "첫 보도", ["할리스", "해킹"], T0)
    m2 = issues.Member(2, "가운데", ["할리스", "개인정보", "유출", "해킹"], T0 + timedelta(hours=2))
    m3 = issues.Member(3, "가운데 늦게", ["할리스", "개인정보", "유출", "해킹"], T0 + timedelta(hours=3))
    assert issues.representative([m1, m2, m3], ["할리스", "개인정보", "유출", "해킹"]).document_id == 2


def test_doc_time_ignores_future_published_dates():
    assert issues.doc_time(None, T0) == T0
    assert issues.doc_time(T0 - timedelta(days=1), T0) == T0 - timedelta(days=1)
    assert issues.doc_time(T0 + timedelta(days=1), T0) == T0   # 시간대 오류로 미래가 된 발행일


# ---------------------------------------------------------------------- 메모리 안 묶기 (두 분석기 모두)

SAME_EVENT = [
    (1, "다카이치 총리, 야스쿠니 신사에 공물 봉납"),
    (2, "日 다카이치, 야스쿠니 공물 봉납…외교부 유감 표명"),
    (3, "정부, 다카이치 야스쿠니 공물 봉납에 깊은 유감"),
]


def test_same_event_from_three_outlets_is_one_issue(analyzer):
    cl = MemoryClusterer(72)
    ids = [cl.add(i, source, title, T0 + timedelta(hours=i)) for i, (source, title) in enumerate(SAME_EVENT)]
    assert len(set(ids)) == 1
    issue = cl.issues[ids[0]]
    assert issue.source_count == 3 and len(issue.members) == 3
    assert {"다카이치", "야스쿠니", "봉납"} <= set(issue.top[:4])   # 세 제목 모두에 나온 낱말이 서명 앞에 온다
    assert issue.first_seen == T0 and issue.last_seen == T0 + timedelta(hours=2)


def test_same_outlet_duplicates_count_as_documents_not_outlets(analyzer):
    cl = MemoryClusterer(72)
    a = cl.add(1, 1, "할리스 멤버십 해킹, 고객 개인정보 유출", T0)
    b = cl.add(2, 1, "할리스 멤버십 해킹, 고객 개인정보 유출(종합)", T0 + timedelta(hours=1))
    assert a == b and cl.issues[a].source_count == 1 and len(cl.issues[a].members) == 2


def test_unrelated_stories_sharing_a_generic_word_stay_apart(analyzer):
    cl = MemoryClusterer(72)
    a = cl.add(1, 1, "서울 아파트값 88주 연속 상승", T0)
    b = cl.add(2, 2, "서울 지하철 노조 파업 예고", T0 + timedelta(hours=1))
    c = cl.add(3, 3, "[속보] 정부 대통령 오늘 발표", T0 + timedelta(hours=2))   # 흔한 낱말뿐: 이슈를 만들지 않는다
    assert a != b and c is None


def test_window_expiry_closes_issue_and_new_report_starts_another(analyzer):
    cl = MemoryClusterer(72)
    first = cl.add(1, 1, SAME_EVENT[0][1], T0)
    later = T0 + timedelta(hours=80)
    assert cl.close(later) == 1 and cl.issues[first].status == "CLOSED"
    again = cl.add(2, 2, SAME_EVENT[1][1], later, now=later)
    assert again != first and cl.issues[again].status == "OPEN"
