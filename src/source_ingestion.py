import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import partial

from src.bizinfo_client import BizinfoClient
from src.db import log_ingestion_run, upsert_program
from src.fanfandaero_client import FanfandaeroClient
from src.normalizer import (
    normalize_event,
    normalize_fanfandaero_support,
    normalize_sbiz24_support,
    normalize_support,
)
from src.program_state import ProgramRecord
from src.sbiz24_client import Sbiz24Client
from src.source_result import CollectionStatus, CoverageEntry, RawItem, SourceBatch

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IngestionOutcome:
    items: tuple[ProgramRecord, ...]
    coverage: tuple[CoverageEntry, ...]


def _log_ingestion(
    kind: str,
    fetched_count: int,
    normalized_count: int,
    error: str | None = None,
) -> None:
    log_ingestion_run({
        "run_at": datetime.now().isoformat(),
        "kind": kind,
        "fetched_count": fetched_count,
        "new_count": normalized_count,
        "updated_count": 0,
        "error": error,
    })


def _bizinfo_batch(source: str, raw_items: list[RawItem], client=None) -> SourceBatch:
    batches = getattr(client, "collection_batches", None)
    batch = batches.get(source) if isinstance(batches, dict) else None
    if isinstance(batch, SourceBatch):
        return batch

    if not raw_items:
        return SourceBatch(
            source=source,
            items=(),
            status=CollectionStatus.FAILED,
            reported_count=None,
            fetched_count=0,
            note="0건 반환 — 수집 완료 여부를 확인할 메타데이터 없음",
        )

    return SourceBatch(
        source=source,
        items=tuple(raw_items),
        status=CollectionStatus.PARTIAL,
        reported_count=None,
        fetched_count=len(raw_items),
        note="전체 수집 여부 미확인 — 조회 메타데이터 없음",
    )


def _fanfandaero_batch(raw_items: list[RawItem], enabled: bool) -> SourceBatch:
    if not enabled:
        return SourceBatch(
            source="fanfandaero",
            items=(),
            status=CollectionStatus.DISABLED,
            reported_count=None,
            fetched_count=0,
            note="FANFANDAERO_ENABLED=false",
        )
    status = CollectionStatus.SUCCESS if raw_items else CollectionStatus.FAILED
    return SourceBatch(
        source="fanfandaero",
        items=tuple(raw_items),
        status=status,
        reported_count=len(raw_items) if raw_items else None,
        fetched_count=len(raw_items),
        note=None if raw_items else "0건 반환 — 접속 실패 가능",
    )


def _ingest_batch(
    kind: str,
    batch: SourceBatch,
    normalizer: Callable[[RawItem], ProgramRecord],
) -> tuple[list[ProgramRecord], CoverageEntry]:
    normalized_items: list[ProgramRecord] = []
    failed_count = 0
    for item in batch.items:
        try:
            normalized_items.append(normalizer(item))
        except (KeyError, TypeError, ValueError) as error:
            failed_count += 1
            logger.warning(
                "Source item normalization failed source=%s error=%s",
                batch.source,
                type(error).__name__,
            )

    for item in normalized_items:
        upsert_program(item)

    status = batch.status
    note = batch.note
    if failed_count and status is not CollectionStatus.FAILED:
        status = CollectionStatus.PARTIAL
        suffix = f"정규화 실패 {failed_count}건"
        note = f"{note}; {suffix}" if note else suffix

    coverage = CoverageEntry(
        source=batch.source,
        status=status,
        reported_count=batch.reported_count,
        fetched_count=batch.fetched_count,
        unique_count=batch.unique_count,
        normalized_count=len(normalized_items),
        note=note,
    )
    error_note = note if status in (CollectionStatus.PARTIAL, CollectionStatus.FAILED) else None
    _log_ingestion(kind, batch.fetched_count, len(normalized_items), error_note)
    return normalized_items, coverage


def _deduplicate_programs(items: list[ProgramRecord]) -> tuple[ProgramRecord, ...]:
    deduplicated: list[ProgramRecord] = []
    positions: dict[str, int] = {}
    for item in items:
        key = str(item.get("program_key") or "")
        if not key:
            continue
        position = positions.get(key)
        if position is None:
            positions[key] = len(deduplicated)
            deduplicated.append(dict(item))
            continue

        existing = dict(deduplicated[position])
        for field, value in item.items():
            if not existing.get(field) and value not in (None, "", [], {}):
                existing[field] = value
        deduplicated[position] = existing
    return tuple(deduplicated)


def ingest_all(
    client: BizinfoClient,
    fanfandaero_client: FanfandaeroClient | None = None,
    sbiz24_client: Sbiz24Client | None = None,
) -> IngestionOutcome:
    fanfandaero = fanfandaero_client or FanfandaeroClient()
    sbiz24 = sbiz24_client or Sbiz24Client()

    logger.info("Source collection started source=bizinfo_support")
    supports = client.fetch_support_programs()
    logger.info("Source collection started source=bizinfo_event")
    events = client.fetch_events()
    logger.info("Source collection started source=fanfandaero")
    fanfandaero_items = fanfandaero.fetch_support_programs()
    logger.info("Source collection started source=sbiz24")
    sbiz24_own, sbiz24_combined = sbiz24.collect_all()

    sources: tuple[tuple[str, SourceBatch, Callable[[RawItem], ProgramRecord]], ...] = (
        ("support", _bizinfo_batch("bizinfo_support", supports, client), normalize_support),
        ("event", _bizinfo_batch("bizinfo_event", events, client), normalize_event),
        (
            "fanfandaero_support",
            _fanfandaero_batch(fanfandaero_items, getattr(fanfandaero, "enabled", True)),
            normalize_fanfandaero_support,
        ),
        (
            "sbiz24",
            sbiz24_own,
            partial(normalize_sbiz24_support, source="sbiz24"),
        ),
        (
            "sbiz24_combine",
            sbiz24_combined,
            partial(normalize_sbiz24_support, source="sbiz24_combine"),
        ),
    )

    normalized: list[ProgramRecord] = []
    coverage: list[CoverageEntry] = []
    for kind, batch, normalizer in sources:
        source_items, source_coverage = _ingest_batch(kind, batch, normalizer)
        normalized.extend(source_items)
        coverage.append(source_coverage)
        logger.info(
            "Source collection completed source=%s status=%s fetched=%s normalized=%s",
            batch.source,
            source_coverage.status,
            source_coverage.fetched_count,
            source_coverage.normalized_count,
        )

    return IngestionOutcome(
        items=_deduplicate_programs(normalized),
        coverage=tuple(coverage),
    )
