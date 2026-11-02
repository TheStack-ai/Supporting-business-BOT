"""Bounded Bizinfo pagination with explicit collection completeness evidence.

Official API docs distinguish searchCnt (result limit; 0 means all) from
pageUnit (page size) and pageIndex. Never infer a remote limit from 100 rows.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import logging
import os
import time
from typing import Any

import requests
import xmltodict

from src.source_result import CollectionStatus, SourceBatch

logger = logging.getLogger(__name__)

SUPPORT_API_URL = "https://www.bizinfo.go.kr/uss/rss/bizinfoApi.do"
EVENT_API_URL = "https://www.bizinfo.go.kr/uss/rss/bizinfoEventApi.do"
DEFAULT_SEARCH_COUNT = 100  # Backward-compatible name for the page-size default.
DEFAULT_MAX_PAGES = 100
MAX_SECONDS = 300
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 30
MAX_ATTEMPTS = 4
RETRYABLE_HTTP = frozenset({408, 425, 429, 500, 502, 503, 504})


class _PageError(ValueError):
    """Only fixed, credential-free error codes may be logged."""


class _TransportError(RuntimeError):
    pass


def _retry_after_seconds(response) -> float | None:
    value = getattr(response, "headers", {}).get("Retry-After", "")
    if not value:
        return None
    try:
        return max(0, float(value))
    except (ValueError, TypeError):
        try:
            timestamp = parsedate_to_datetime(value)
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            return max(0, timestamp.timestamp() - datetime.now(timezone.utc).timestamp())
        except (ValueError, TypeError, OverflowError):
            return None


@dataclass(frozen=True)
class _Page:
    items: list[dict[str, Any]]
    totals: frozenset[int]


def _totals(value: Any) -> frozenset[int]:
    totals: set[int] = set()
    if isinstance(value, list):
        for item in value:
            totals.update(_totals(item))
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in ("totCnt", "totalCount") and item not in (None, ""):
                if isinstance(item, bool):
                    raise _PageError("invalid_total")
                try:
                    count = int(str(item).replace(",", ""))
                except (TypeError, ValueError):
                    raise _PageError("invalid_total") from None
                if count < 0:
                    raise _PageError("invalid_total")
                totals.add(count)
            elif key in ("jsonArray", "rss", "channel", "item", "items", "response", "body", "data"):
                totals.update(_totals(item))
    return frozenset(totals)


def _item_list(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise _PageError("invalid_items")
    return value


class BizinfoClient:
    def __init__(self):
        self.support_key = os.getenv("BIZINFO_SUPPORT_KEY")
        self.event_key = os.getenv("BIZINFO_EVENT_KEY")
        self._session = requests.Session()
        # List-returning methods remain compatible. Ingestion consumes evidence
        # in these batches instead of guessing from the returned list length.
        self.collection_batches: dict[str, SourceBatch] = {}
        self.collection_metadata: dict[str, dict[str, Any]] = {}

    def _request(self, source, url, params, deadline, attempts):
        """Retry the same page; diagnostics contain no URL, key, body or exception text."""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _TransportError("time_cap")
            # Separate the connection and response budgets. Requests' read
            # timeout is inactivity between bytes, not a total wall-clock timer.
            connect = min(CONNECT_TIMEOUT, remaining / 2)
            timeout = (connect, min(READ_TIMEOUT, max(0.001, remaining - connect)))
            started = time.monotonic()
            response = None
            error_kind, retryable = None, False
            try:
                response = self._session.get(url, params=params, timeout=timeout)
                response.raise_for_status()
            except requests.exceptions.SSLError:
                error_kind = "tls_error"
            except requests.ConnectTimeout:
                error_kind, retryable = "connect_timeout", True
            except requests.ReadTimeout:
                error_kind, retryable = "read_timeout", True
            except requests.Timeout:
                error_kind, retryable = "timeout", True
            except requests.HTTPError as error:
                if response is None:
                    response = error.response
                status = getattr(response, "status_code", None)
                error_kind, retryable = "http_error", status in RETRYABLE_HTTP
            except requests.ConnectionError:
                error_kind, retryable = "connection_error", True
            except requests.RequestException:
                error_kind = "request_error"
            record = {"page": params["pageIndex"], "format": params["dataType"],
                      "attempt": attempt, "elapsed_seconds": round(time.monotonic() - started, 3),
                      "http_status": getattr(response, "status_code", None),
                      "error_kind": error_kind}
            attempts.append(record)
            if error_kind is None:
                return response
            logger.warning("Bizinfo request source=%s page=%s attempt=%s error=%s http=%s elapsed=%s",
                           source, record["page"], attempt, error_kind, record["http_status"], record["elapsed_seconds"])
            wait = max(2 ** (attempt - 1), _retry_after_seconds(response) or 0)
            remaining = deadline - time.monotonic()
            if not retryable or attempt == MAX_ATTEMPTS:
                raise _TransportError("transport_error")
            if wait >= remaining:
                record["retry_stopped"] = "insufficient_time_budget"
                raise _TransportError("time_cap")
            record["retry_wait_seconds"] = wait
            time.sleep(wait)
        raise _TransportError("transport_error")

    def _search_count(self) -> int:
        """Page size; the old environment name remains a deployment alias."""
        try:
            value = os.getenv("BIZINFO_PAGE_UNIT") or os.getenv("BIZINFO_SEARCH_COUNT", str(DEFAULT_SEARCH_COUNT))
            return max(1, min(int(value), 1000))
        except ValueError:
            return DEFAULT_SEARCH_COUNT

    def _max_pages(self) -> int:
        try:
            return max(1, min(int(os.getenv("BIZINFO_MAX_PAGES", str(DEFAULT_MAX_PAGES))), 200))
        except ValueError:
            return DEFAULT_MAX_PAGES

    def _item_key(self, item: dict[str, Any]) -> str:
        for key in ("pblancId", "eventInfoId", "eventId", "nttId", "seq", "inqireUrl", "pblancUrl", "originUrlAdres", "orginlUrlAdres", "link"):
            value = item.get(key)
            if value:
                return f"{key}:{value}"
        # A changing count must not create a new identity for the same notice.
        fields = {key: value for key, value in item.items() if key not in ("totCnt", "totalCount")}
        return json.dumps(fields, ensure_ascii=False, sort_keys=True)

    def _json_page(self, response: requests.Response) -> _Page:
        try:
            payload = response.json()
        except ValueError:
            raise _PageError("json_decode") from None
        if isinstance(payload, dict) and "reqErr" in payload:
            raise _PageError("api_error")
        if not isinstance(payload, dict) or "jsonArray" not in payload:
            raise _PageError("json_envelope")
        return _Page(_item_list(payload["jsonArray"]), _totals(payload))

    def _xml_page(self, response: requests.Response) -> _Page:
        try:
            payload = xmltodict.parse(response.content)
        except Exception:
            raise _PageError("xml_decode") from None
        channel = payload.get("rss", {}).get("channel") if isinstance(payload, dict) else None
        if not isinstance(channel, dict):
            raise _PageError("xml_envelope")
        return _Page(_item_list(channel.get("item")), _totals(payload))

    def _fetch(self, url: str, api_key: str | None, params: dict | None = None) -> list[dict[str, Any]]:
        source = "bizinfo_event" if url == EVENT_API_URL else "bizinfo_support"
        results: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        reported_count: int | None = None
        received = pages = 0
        attempts: list[dict[str, Any]] = []
        page_receipts: list[dict[str, Any]] = []
        response_format = "json"
        reason = "missing_key" if not api_key else "page_cap"
        complete = False
        page_size = self._search_count()
        deadline = time.monotonic() + MAX_SECONDS
        base_params = dict(params or {})
        base_params.update(crtfcKey=api_key, searchCnt=0, pageUnit=page_size)

        if api_key:
            for page_index in range(1, self._max_pages() + 1):
                if time.monotonic() >= deadline:
                    reason = "time_cap"
                    break
                page_params = {**base_params, "dataType": response_format, "pageIndex": page_index}
                try:
                    response = self._request(source, url, page_params, deadline, attempts)
                    try:
                        page = self._json_page(response) if response_format == "json" else self._xml_page(response)
                    except _PageError as error:
                        # Retry a JSON decode failure as RSS, never an HTTP/auth
                        # failure or an arbitrary malformed JSON envelope.
                        if page_index != 1 or response_format != "json" or str(error) != "json_decode":
                            raise
                        if time.monotonic() >= deadline:
                            reason = "time_cap"
                            break
                        response_format = "rss"
                        page_params["dataType"] = "rss"
                        response = self._request(source, url, page_params, deadline, attempts)
                        page = self._xml_page(response)
                except _TransportError as error:
                    reason = str(error)
                    break
                except _PageError as error:
                    reason = str(error)
                    break

                pages += 1
                received += len(page.items)
                added = 0
                for item in page.items:
                    key = self._item_key(item)
                    if key not in seen_keys:
                        seen_keys.add(key)
                        results.append(item)
                        added += 1
                page_receipts.append({"page": page_index, "received": len(page.items),
                                      "added_unique": added, "cumulative_unique": len(results)})

                if len(page.totals) > 1:
                    reason = "total_changed"
                    break
                page_total = next(iter(page.totals), None)
                if reported_count is not None and page_total is not None and page_total != reported_count:
                    reason = "total_changed"
                    break
                if reported_count is None and page_total is not None:
                    reported_count = page_total
                if reported_count is not None and len(results) > reported_count:
                    reason = "count_mismatch"
                    break
                if reported_count is not None and len(results) == reported_count:
                    reason, complete = "reported_total_reached", True
                    break
                if not page.items:
                    if reported_count is not None:
                        reason = "count_mismatch"
                    elif response_format == "rss":
                        reason = "xml_total_unknown"
                    elif not results:
                        reason = "empty_total_unknown"
                    else:
                        reason, complete = "empty_final_page", True
                    break
                if added == 0:
                    reason = "repeated_page"
                    break
                if len(page.items) < page_size:
                    if reported_count is not None:
                        reason = "truncated_page"
                    elif response_format == "rss":
                        reason = "xml_total_unknown"
                    else:
                        reason, complete = "short_final_page", True
                    break

        notes = {
            "missing_key": "API 인증키 미설정",
            "transport_error": "조회 중 통신 오류 — 수집 범위 확인 필요",
            "json_decode": "API 응답 해석 실패",
            "json_envelope": "API 응답 구조 확인 필요",
            "api_error": "API 오류 응답 — 조회 조건·인증 상태 확인 필요",
            "xml_decode": "XML 응답 해석 실패",
            "xml_envelope": "XML 응답 구조 확인 필요",
            "invalid_items": "API 항목 구조 확인 필요",
            "invalid_total": "API 전체건수 값 확인 필요",
            "total_changed": "조회 도중 전체건수 변경 — 동일 시점의 전수 수집 미확인",
            "count_mismatch": "API 전체건수와 수집한 고유 공고 수 불일치",
            "truncated_page": "전체건수에 도달하기 전 짧은 페이지 반환",
            "repeated_page": "같은 공고 페이지 반복 — 이후 범위 미확인",
            "page_cap": "안전 페이지 한도 도달 — 이후 범위 미확인",
            "time_cap": "수집 시간 한도 도달 — 이후 범위 미확인",
            "xml_total_unknown": "XML 대체 조회 — 전체건수 미확인",
            "empty_total_unknown": "빈 결과 반환 — 전체건수와 정상 완료 여부 미확인",
        }
        status = CollectionStatus.SUCCESS if complete else (CollectionStatus.PARTIAL if results else CollectionStatus.FAILED)
        note = None if complete else notes[reason]
        if reason == "transport_error" and attempts:
            last = attempts[-1]
            detail = {"read_timeout": "응답 대기 시간 초과", "connect_timeout": "연결 시간 초과",
                      "timeout": "요청 시간 초과", "connection_error": "연결 끊김",
                      "tls_error": "TLS 연결 검증 실패", "request_error": "요청 설정 오류"}.get(last["error_kind"])
            if last["error_kind"] == "http_error":
                detail = "HTTP " + str(last["http_status"])
            note = f"{last['page']}페이지 {detail or '통신 오류'} — {last['attempt']}회 시도 후 중단"
        self.collection_batches[source] = SourceBatch(
            source=source, items=tuple(results), status=status,
            reported_count=reported_count, fetched_count=received, note=note,
        )
        self.collection_metadata[source] = {
            "status": status.value, "reported_count": reported_count,
            "fetched_count": received, "unique_count": len(results),
            "pages": pages, "format": response_format, "termination": reason,
            "attempts": attempts, "page_receipts": page_receipts,
            "request_count": len(attempts),
            "retry_count": sum(record["attempt"] > 1 for record in attempts),
            "failed_attempt_count": sum(record["error_kind"] is not None for record in attempts),
        }
        logger.info("Bizinfo collection source=%s status=%s pages=%s unique=%s reported=%s termination=%s requests=%s retries=%s",
                    source, status.value, pages, len(results), reported_count, reason, len(attempts),
                    self.collection_metadata[source]["retry_count"])
        return results

    def fetch_support_programs(self) -> list[dict[str, Any]]:
        return self._fetch(SUPPORT_API_URL, self.support_key)

    def fetch_events(self) -> list[dict[str, Any]]:
        return self._fetch(EVENT_API_URL, self.event_key)
