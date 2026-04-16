# tests/test_run_once_v2.py
from src.llm_filter import Assessment


def _make_program(key: str, title: str) -> dict:
    return {
        "program_key": key,
        "kind": "support",
        "source": "bizinfo",
        "seq": key.split(":")[1],
        "title": title,
        "summary_raw": "요약",
        "agency": "기관",
        "category_l1": None,
        "region_raw": "경기",
        "apply_period_raw": None,
        "apply_start_at": None,
        "apply_end_at": "2026-05-01",
        "url": "https://example.com",
        "created_at_source": None,
        "ingested_at": "2026-04-16",
    }


def test_graded_notification_format():
    from src.run_once import format_graded_message

    grade_a = [
        (_make_program("s:1", "보안장비 지원"), Assessment("A", "KC 인증 대상, 1억", "충족")),
    ]
    grade_b = [
        (_make_program("s:2", "디지털전환 컨설팅"), Assessment("B", "SW사업자 가능", "미확인")),
    ]
    msg = format_graded_message(grade_a, grade_b, total_checked=50, stage1_passed=10)
    assert "🔴" in msg
    assert "보안장비 지원" in msg
    assert "KC 인증 대상" in msg
    assert "🟡" in msg
    assert "디지털전환 컨설팅" in msg


def test_graded_notification_b_only_heading():
    """When only B-grade exists, heading should be '참고 사항' (softer tone)."""
    from src.run_once import format_graded_message

    grade_b = [
        (_make_program("s:1", "일반 지원사업"), Assessment("B", "가능성 있음", "미확인")),
    ]
    msg = format_graded_message([], grade_b, total_checked=10, stage1_passed=3)
    assert "참고 사항" in msg
    assert "🔴" not in msg


def test_graded_notification_no_results():
    from src.run_once import format_graded_message

    msg = format_graded_message([], [], total_checked=50, stage1_passed=5)
    assert "✅" in msg
    assert "50" in msg


def test_fallback_message_has_warning():
    from src.run_once import format_fallback_message

    items = [
        {"item": _make_program("s:1", "테스트"), "score": 45, "reasons": ["관심분야 일치"]},
    ]
    msg = format_fallback_message(items)
    assert "⚠️" in msg
    assert "테스트" in msg
