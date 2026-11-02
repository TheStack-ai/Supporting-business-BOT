from src.llm_filter import (
    Assessment,
    EligibilityVerdict,
    parse_stage1_response,
    parse_stage2_response,
)


def test_assessment_dataclass():
    a = Assessment(grade="A", reason="자격 충족", eligibility="충족")
    assert a.grade == "A"


def test_parse_stage1_valid():
    raw = {"results": [
        {"id": 1, "decision": "PASS"},
        {"id": 2, "decision": "REJECT"},
        {"id": 3, "decision": "PASS"},
    ]}
    result = parse_stage1_response(raw, total_count=3)
    assert result == {1: "PASS", 2: "REJECT", 3: "PASS"}


def test_parse_stage1_missing_entry_remains_review():
    raw = {"results": [
        {"id": 1, "decision": "REJECT"},
    ]}
    result = parse_stage1_response(raw, total_count=2)
    assert result[1] == "REJECT"
    assert result[2] == "REVIEW"


def test_parse_stage1_garbage_remains_review():
    result = parse_stage1_response("garbage", total_count=3)
    assert all(v == "REVIEW" for v in result.values())


def test_parse_stage2_valid():
    raw = {
        "grade": "A",
        "reason": "KC 인증 보유 대상",
        "eligibility": "충족",
        "verdict": "확인됨",
    }
    a = parse_stage2_response(raw)
    assert a.grade == "A"
    assert a.reason == "KC 인증 보유 대상"
    assert a.eligibility == "충족"
    assert a.verdict is EligibilityVerdict.CONFIRMED


def test_parse_stage2_invalid_grade_remains_review():
    raw = {"grade": "X", "reason": "test", "eligibility": "미확인"}
    a = parse_stage2_response(raw)
    assert a.grade == "REVIEW"
    assert a.review_pending


def test_parse_stage2_never_infers_confirmed_without_explicit_verdict():
    raw = {"grade": "A", "reason": "본문 요건 충족", "eligibility": "충족"}

    assessment = parse_stage2_response(raw)

    assert assessment.verdict is EligibilityVerdict.NEEDS_CONFIRMATION


def test_parse_stage2_garbage():
    a = parse_stage2_response("not a dict")
    assert a.grade == "REVIEW"
    assert a.review_pending
    assert a.eligibility == "미확인"
    assert a.verdict is EligibilityVerdict.NEEDS_CONFIRMATION


def test_parse_stage2_accepts_each_eligibility_verdict():
    verdicts = [
        "확인됨",
        "조건부",
        "확인 필요",
        "신청 불가",
        "사업전환 후보",
    ]

    parsed = [
        parse_stage2_response({
            "grade": "B",
            "reason": "근거",
            "eligibility": "미확인",
            "verdict": verdict,
        }).verdict.value
        for verdict in verdicts
    ]

    assert parsed == verdicts


def test_stage1_api_failure_keeps_relevant_programs_pending(monkeypatch):
    """No failed evaluation is promoted to a recommendation or discarded."""
    from src import llm_filter
    from unittest.mock import MagicMock
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = RuntimeError("API down")
    monkeypatch.setattr(llm_filter, "_get_gemini_client", lambda: mock_client)

    programs = [
        {"title": "KC 인증 지원", "summary_raw": "제조기업 대상", "program_key": "s:1"},
        {"title": "볼라드 실증 지원", "summary_raw": "차량방호 제품", "program_key": "s:2"},
    ]
    from src.llm_filter import stage1_quick_filter
    result = stage1_quick_filter(programs)
    assert len(result) == 0
    assert result.pending_keys == {"s:1", "s:2"}
    assert result.rejected_keys == set()


def test_stage2_empty_detail_is_pending_without_api():
    from src.llm_filter import stage2_assess
    # stage2_assess checks detail_text before calling API, so no mock needed
    program = {"title": "볼라드 실증 지원", "program_key": "s:1"}
    result = stage2_assess(program, "")
    assert result.grade == "REVIEW"
    assert result.review_pending
    assert "접근 불가" in result.reason
