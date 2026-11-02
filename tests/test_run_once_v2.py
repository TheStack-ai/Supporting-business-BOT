# tests/test_run_once_v2.py
from src.llm_filter import Assessment, EligibilityVerdict
from src.program_state import ProgramRecord


def _make_program(key: str, title: str) -> ProgramRecord:
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
        (
            _make_program("s:1", "보안장비 지원"),
            Assessment(
                "A",
                "KC 인증 대상, 1억",
                "충족",
                EligibilityVerdict.CONFIRMED,
            ),
        ),
    ]
    grade_b = [
        (_make_program("s:2", "디지털전환 컨설팅"), Assessment("B", "SW사업자 가능", "미확인")),
    ]
    msg = format_graded_message(grade_a, grade_b, total_checked=50, stage1_passed=10)
    assert "우선 검토 1건" in msg
    assert "지원사업 · 기업마당" in msg
    assert "보안장비 지원" in msg
    assert "KC 인증 대상" in msg
    assert "확인됨" in msg
    assert "요건 확인 1건" in msg
    assert "디지털전환 컨설팅" in msg


def test_graded_notification_marks_changed_deadline():
    from src.run_once import format_graded_message

    changed = _make_program("s:1", "보안장비 지원")
    changed["_change_kind"] = "CHANGED"
    changed["_changed_fields"] = ["apply_end_at"]

    msg = format_graded_message(
        [(changed, Assessment("A", "마감 연장", "충족"))],
        [],
        total_checked=1,
        stage1_passed=1,
    )

    assert "변경" in msg
    assert "마감" in msg


def test_graded_notification_b_only_heading():
    """Conditional notices clearly name the action without an urgent recommendation."""
    from src.run_once import format_graded_message

    grade_b = [
        (_make_program("s:1", "일반 지원사업"), Assessment("B", "가능성 있음", "미확인")),
    ]
    msg = format_graded_message([], grade_b, total_checked=10, stage1_passed=3)
    assert "요건 확인" in msg
    assert "우선 검토" not in msg


def test_graded_notification_no_results():
    from src.run_once import format_graded_message

    msg = format_graded_message([], [], total_checked=50, stage1_passed=5)
    assert "새로 추천할 공고가 없습니다" in msg
    assert "50" in msg


def test_fallback_message_has_warning():
    from src.run_once import format_fallback_message

    items = [
        {"item": _make_program("s:1", "테스트"), "score": 45, "reasons": ["관심분야 일치"]},
    ]
    msg = format_fallback_message(items)
    assert "상세 검토를 완료하지 못해" in msg
    assert "요건 확인" in msg
    assert "테스트" in msg


def test_keyword_fallback_skips_hard_rejected_items():
    from src.run_once import _run_keyword_fallback

    profile = {
        "interests": '["홈쇼핑", "MRO"]',
        "include_keywords": '["홈쇼핑", "MRO"]',
        "exclude_keywords": "[]",
        "region_allow": '["전국"]',
        "min_score": 10,
        "due_days_threshold": 7,
    }
    items = [
        {"program_key": "x:1", "title": "TV홈쇼핑 입점지원", "summary_raw": "온라인쇼핑몰"},
        {"program_key": "x:2", "title": "MRO 공공조달 컨설팅", "summary_raw": "입찰 지원"},
    ]

    recs = _run_keyword_fallback(items, profile)

    assert [rec["item"]["program_key"] for rec in recs] == ["x:2"]


def test_priority_prefers_changed_then_urgent_programs():
    from src.program_selection import prioritize_programs

    routine = _make_program("s:1", "차량방호 볼라드 상시 공고")
    routine["apply_end_at"] = None
    urgent = _make_program("s:2", "차량방호 볼라드 마감 임박")
    urgent["apply_end_at"] = "2026-07-24"
    changed = _make_program("s:3", "차량방호 볼라드 변경 공고")
    changed["_change_kind"] = "CHANGED"

    selected = prioritize_programs([routine, urgent, changed], limit=2)

    assert [item["program_key"] for item in selected] == ["s:3", "s:2"]


def test_long_notification_is_split_without_losing_tail_or_coverage():
    from src.notification_format import messages_with_coverage

    body = "\n\n".join(f"공고 {index}: " + "가" * 250 for index in range(30))
    coverage = "[수집 범위] sbiz24 success 497/497"

    messages = messages_with_coverage(body, coverage, max_length=1000)

    assert len(messages) > 1
    assert all(len(message) <= 1000 for message in messages)
    assert "공고 29" in "".join(messages)
    assert messages[-1].endswith(coverage)
