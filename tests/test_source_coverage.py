from src.source_result import CollectionStatus, CoverageEntry
from src.source_coverage import format_coverage_manifest, format_coverage_notice, has_degraded_sources


def test_coverage_manifest_exposes_failed_source():
    coverage = (
        CoverageEntry(
            source="bizinfo_support",
            status=CollectionStatus.FAILED,
            reported_count=None,
            fetched_count=0,
            unique_count=0,
            normalized_count=0,
            note="연결 시간 초과",
        ),
        CoverageEntry(
            source="sbiz24",
            status=CollectionStatus.SUCCESS,
            reported_count=497,
            fetched_count=497,
            unique_count=497,
            normalized_count=497,
        ),
    )

    message = format_coverage_manifest(coverage)

    assert "수집 현황" in message
    assert "coverage_manifest" not in message
    assert "기업마당 지원사업" in message
    assert "실패" in message
    assert "연결 시간 초과" in message
    assert "497/497" in message
    assert has_degraded_sources(coverage) is True


def entry(source, status=CollectionStatus.SUCCESS, note=None):
    return CoverageEntry(source, status, 1280, 1280, 1160, 1160, note)


def test_compact_notice_omits_internal_row_and_duplicate_counts():
    coverage = tuple(entry(source) for source in (
        "bizinfo_support", "bizinfo_event", "fanfandaero", "sbiz24", "sbiz24_combine",
    ))
    assert format_coverage_notice(coverage) == "수집 범위 · 5개 출처 확인"
    # The full operational summary still contains the diagnostic counts.
    assert "1280/1280행, 고유 1160건" in format_coverage_manifest(coverage)


def test_compact_notice_does_not_count_disabled_sources_as_confirmed():
    coverage = (entry("bizinfo_support"), entry("bizinfo_event", CollectionStatus.DISABLED))
    assert format_coverage_notice(coverage) == "수집 범위 · 1개 출처 확인 · 1개 미사용"
    assert format_coverage_notice((entry("bizinfo_support", CollectionStatus.DISABLED),)) == "수집 범위 · 활성화된 출처 없음"
    assert format_coverage_notice(()) == "수집 범위 · 확인된 출처 없음"


def test_compact_notice_keeps_partial_and_failed_source_scope_visible():
    coverage = (
        entry("bizinfo_support", CollectionStatus.PARTIAL, "API 페이지 상한/반복 감지 — 전수 아님"),
        entry("bizinfo_event", CollectionStatus.FAILED, "연결 시간 초과"),
        entry("sbiz24"),
    )
    text = format_coverage_notice(coverage)
    assert text.startswith("수집 범위 · 1개 출처 확인 · 2개 확인 필요")
    assert "기업마당 지원사업: 일부만 확인했습니다 · 추가 공고 여부 확인 필요" in text
    assert "기업마당 행사: 연결 지연으로 목록을 확인하지 못했습니다" in text
    assert "API" not in text and "1280" not in text
