from src.llm_filter import (
    Assessment,
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


def test_parse_stage1_missing_entry_defaults_pass():
    raw = {"results": [
        {"id": 1, "decision": "REJECT"},
    ]}
    result = parse_stage1_response(raw, total_count=2)
    assert result[1] == "REJECT"
    assert result[2] == "PASS"


def test_parse_stage1_garbage_defaults_all_pass():
    result = parse_stage1_response("garbage", total_count=3)
    assert all(v == "PASS" for v in result.values())


def test_parse_stage2_valid():
    raw = {"grade": "A", "reason": "KC 인증 보유 대상", "eligibility": "충족"}
    a = parse_stage2_response(raw)
    assert a.grade == "A"
    assert a.reason == "KC 인증 보유 대상"
    assert a.eligibility == "충족"


def test_parse_stage2_invalid_grade_defaults_b():
    raw = {"grade": "X", "reason": "test", "eligibility": "미확인"}
    a = parse_stage2_response(raw)
    assert a.grade == "B"


def test_parse_stage2_garbage():
    a = parse_stage2_response("not a dict")
    assert a.grade == "B"
    assert a.eligibility == "미확인"


def test_stage1_returns_all_on_api_failure(monkeypatch):
    """When Gemini API fails, all programs should PASS (recall-first)."""
    from src import llm_filter
    from unittest.mock import MagicMock
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = RuntimeError("API down")
    monkeypatch.setattr(llm_filter, "_get_gemini_client", lambda: mock_client)

    programs = [
        {"title": "A", "summary_raw": "test", "program_key": "s:1"},
        {"title": "B", "summary_raw": "test", "program_key": "s:2"},
    ]
    from src.llm_filter import stage1_quick_filter
    result = stage1_quick_filter(programs)
    assert len(result) == 2


def test_stage2_empty_detail_returns_grade_b():
    """Empty detail text should return B grade without calling API."""
    from src.llm_filter import stage2_assess
    # stage2_assess checks detail_text before calling API, so no mock needed
    program = {"title": "테스트", "program_key": "s:1"}
    result = stage2_assess(program, "")
    assert result.grade == "B"
    assert "접근 불가" in result.reason
