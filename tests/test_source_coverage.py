from src.source_result import CollectionStatus, CoverageEntry
from src.source_coverage import format_coverage_manifest, has_degraded_sources


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
