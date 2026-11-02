from typing import Final, assert_never
from pathlib import Path

from src.source_result import CollectionStatus, CoverageEntry

_SOURCE_LABELS: Final[dict[str, str]] = {
    "bizinfo_support": "기업마당 지원사업",
    "bizinfo_event": "기업마당 행사",
    "fanfandaero": "판판대로",
    "sbiz24": "소상공인24 소진공 공고",
    "sbiz24_combine": "소상공인24 통합조회",
}


def _status_label(status: CollectionStatus) -> str:
    match status:
        case CollectionStatus.SUCCESS:
            return "확인"
        case CollectionStatus.PARTIAL:
            return "일부 확인"
        case CollectionStatus.FAILED:
            return "수집 실패"
        case CollectionStatus.DISABLED:
            return "미사용"
        case unreachable:
            assert_never(unreachable)


def _count_label(entry: CoverageEntry) -> str:
    if entry.reported_count is None:
        return f"{entry.normalized_count}건"
    if entry.unique_count == entry.fetched_count:
        return f"{entry.normalized_count}/{entry.reported_count}"
    return (
        f"{entry.fetched_count}/{entry.reported_count}행, "
        f"고유 {entry.normalized_count}건"
    )


def format_coverage_manifest(entries: tuple[CoverageEntry, ...]) -> str:
    lines = ["수집 현황"]
    for entry in entries:
        label = _SOURCE_LABELS.get(entry.source, entry.source)
        line = f"{label}: {_status_label(entry.status)} · {_count_label(entry)}"
        if entry.note:
            note = entry.note.replace("API 페이지 상한/반복 감지 — 전수 아님", "추가 공고 여부 확인 필요")
            note = note.replace("0건 반환 — 접속 실패 또는 API 오류 가능", "목록을 확인하지 못했습니다")
            line += f" — {note}"
        lines.append(line)
    return "\n".join(lines)


def _coverage_problem(entry: CoverageEntry) -> str:
    note = (entry.note or "").lower()
    if entry.status is CollectionStatus.PARTIAL:
        if "정규화" in note or "변환" in note:
            return "일부 공고 정보를 처리하지 못했습니다"
        if "시간 초과" in note or "timeout" in note:
            return "연결 지연으로 일부 공고만 확인했습니다"
        return "일부만 확인했습니다 · 추가 공고 여부 확인 필요"
    if "시간 초과" in note or "timeout" in note:
        return "연결 지연으로 목록을 확인하지 못했습니다"
    if "인증" in note or "키 누락" in note or "key_missing" in note or "unauthorized" in note:
        return "접속 설정을 확인해야 합니다"
    return "목록을 확인하지 못했습니다"


def format_coverage_notice(entries: tuple[CoverageEntry, ...]) -> str:
    """Compact user notice; detailed row counts remain in the operational manifest."""
    if not entries:
        return "수집 범위 · 확인된 출처 없음"
    confirmed = sum(entry.status is CollectionStatus.SUCCESS for entry in entries)
    inactive = sum(entry.status is CollectionStatus.DISABLED for entry in entries)
    problems = [entry for entry in entries
                if entry.status in (CollectionStatus.PARTIAL, CollectionStatus.FAILED)]
    if inactive == len(entries):
        return "수집 범위 · 활성화된 출처 없음"
    header = f"수집 범위 · {confirmed}개 출처 확인"
    if problems:
        header += f" · {len(problems)}개 확인 필요"
    if inactive:
        header += f" · {inactive}개 미사용"
    lines = [header]
    for entry in problems:
        label = _SOURCE_LABELS.get(entry.source, entry.source)
        lines.append(f"{label}: {_coverage_problem(entry)}")
    return "\n".join(lines)


def has_degraded_sources(entries: tuple[CoverageEntry, ...]) -> bool:
    return any(
        entry.status in (CollectionStatus.PARTIAL, CollectionStatus.FAILED)
        for entry in entries
    )


def write_github_step_summary(coverage_manifest: str, path: str) -> None:
    summary_path = Path(path)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    with summary_path.open("a", encoding="utf-8") as summary:
        summary.write("## 지원사업 수집 현황\n\n")
        summary.write(coverage_manifest + "\n")
