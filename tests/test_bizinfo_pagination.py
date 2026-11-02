import json
import logging

import pytest
import requests

from src.bizinfo_client import BizinfoClient
from src.source_ingestion import _bizinfo_batch
from src.source_result import CollectionStatus


class Response:
    def __init__(self, payload=None, xml=None):
        self.payload = payload
        self.content = (xml or "").encode()

    def raise_for_status(self):
        return None

    def json(self):
        if self.payload is None:
            raise json.JSONDecodeError("not json", "", 0)
        return self.payload


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("BIZINFO_SUPPORT_KEY", "fake-private-key")
    monkeypatch.setenv("BIZINFO_EVENT_KEY", "fake-event-key")
    monkeypatch.setenv("BIZINFO_PAGE_UNIT", "100")
    monkeypatch.delenv("BIZINFO_MAX_PAGES", raising=False)
    monkeypatch.setattr("src.bizinfo_client.time.sleep", lambda _seconds: None)
    return BizinfoClient()


def items(start, count, total=None):
    return [{"pblancId": str(i), **({"totCnt": str(total)} if total is not None else {})}
            for i in range(start, start + count)]


def install_pages(monkeypatch, pages):
    calls = []
    def get(_session, _url, params, timeout):
        calls.append(dict(params))
        result = pages[min(len(calls) - 1, len(pages) - 1)]
        if isinstance(result, Exception):
            raise result
        return result if isinstance(result, Response) else Response(result)
    monkeypatch.setattr("src.bizinfo_client.requests.Session.get", get)
    return calls


def batch(client):
    return client.collection_batches["bizinfo_support"]


def termination(client):
    return client.collection_metadata["bizinfo_support"]["termination"]


def test_more_than_100_is_complete_when_reported_total_is_reached(client, monkeypatch):
    calls = install_pages(monkeypatch, [
        {"jsonArray": items(0, 100, 250)},
        {"totCnt": "250", "jsonArray": items(100, 100)},
        {"jsonArray": items(200, 50, 250)},
    ])
    result = client.fetch_support_programs()
    assert len(result) == 250
    assert batch(client).status is CollectionStatus.SUCCESS
    assert batch(client).reported_count == batch(client).fetched_count == 250
    assert batch(client).note is None
    assert [call["pageIndex"] for call in calls] == [1, 2, 3]
    assert all(call["searchCnt"] == 0 and call["pageUnit"] == 100 for call in calls)
    assert _bizinfo_batch("bizinfo_support", result, client) is batch(client)


def test_repeated_page_keeps_unique_items_and_exposes_partial(client, monkeypatch):
    page = {"jsonArray": items(0, 100, 300)}
    calls = install_pages(monkeypatch, [page, page])
    assert len(client.fetch_support_programs()) == 100
    assert len(calls) == 2
    assert batch(client).status is CollectionStatus.PARTIAL
    assert batch(client).fetched_count == 200
    assert termination(client) == "repeated_page"


def test_short_page_before_reported_total_is_not_success(client, monkeypatch):
    install_pages(monkeypatch, [{"jsonArray": items(0, 50, 250)}])
    assert len(client.fetch_support_programs()) == 50
    assert batch(client).status is CollectionStatus.PARTIAL
    assert termination(client) == "truncated_page"


def test_changed_total_during_pagination_is_visible(client, monkeypatch):
    install_pages(monkeypatch, [
        {"jsonArray": items(0, 100, 250)},
        {"jsonArray": items(100, 100, 260)},
    ])
    assert len(client.fetch_support_programs()) == 200
    assert batch(client).status is CollectionStatus.PARTIAL
    assert batch(client).reported_count == 250
    assert termination(client) == "total_changed"


@pytest.mark.parametrize("last_page", [
    {"jsonArray": [], "totCnt": 200},
    {"jsonArray": items(100, 100, 150)},
])
def test_count_mismatch_or_changed_total_cannot_be_success(client, monkeypatch, last_page):
    install_pages(monkeypatch, [{"jsonArray": items(0, 100, 200)}, last_page])
    client.fetch_support_programs()
    assert batch(client).status is CollectionStatus.PARTIAL
    assert termination(client) in ("count_mismatch", "total_changed")


def test_zero_total_with_items_is_count_mismatch(client, monkeypatch):
    install_pages(monkeypatch, [{"totCnt": 0, "jsonArray": items(0, 1)}])
    client.fetch_support_programs()
    assert batch(client).status is CollectionStatus.PARTIAL
    assert termination(client) == "count_mismatch"


def test_explicit_zero_total_is_successful_empty_collection(client, monkeypatch):
    install_pages(monkeypatch, [{"totCnt": 0, "jsonArray": []}])
    assert client.fetch_support_programs() == []
    assert batch(client).status is CollectionStatus.SUCCESS
    assert batch(client).reported_count == 0
    assert _bizinfo_batch("bizinfo_support", [], client).status is CollectionStatus.SUCCESS


def test_empty_without_completion_evidence_is_not_claimed_success(client, monkeypatch):
    install_pages(monkeypatch, [{"jsonArray": []}])
    assert client.fetch_support_programs() == []
    assert batch(client).status is CollectionStatus.FAILED
    assert termination(client) == "empty_total_unknown"


def test_transport_error_is_distinct_and_never_logs_access_url(client, monkeypatch, caplog):
    secret_url = "https://example.test/api?crtfcKey=fake-private-key"
    install_pages(monkeypatch, [requests.Timeout(secret_url)])
    with caplog.at_level(logging.INFO):
        assert client.fetch_support_programs() == []
    assert batch(client).status is CollectionStatus.FAILED
    assert termination(client) == "transport_error"
    assert secret_url not in caplog.text and "fake-private-key" not in caplog.text


def test_transport_failure_after_one_page_preserves_partial_items(client, monkeypatch):
    install_pages(monkeypatch, [{"jsonArray": items(0, 100, 250)}, requests.ConnectionError("secret-url")])
    assert len(client.fetch_support_programs()) == 100
    assert batch(client).status is CollectionStatus.PARTIAL
    assert termination(client) == "transport_error"


def test_xml_fallback_without_total_does_not_claim_full_collection(client, monkeypatch):
    calls = install_pages(monkeypatch, [
        Response(), Response(xml="<rss><channel><item><seq>one</seq></item></channel></rss>"),
    ])
    assert client.fetch_support_programs() == [{"seq": "one"}]
    assert calls[1]["dataType"] == "rss"
    assert calls[1]["pageIndex"] == 1 and calls[1]["pageUnit"] == 100
    assert batch(client).status is CollectionStatus.PARTIAL
    assert termination(client) == "xml_total_unknown"


def test_xml_fallback_can_verify_total_across_pages(client, monkeypatch):
    monkeypatch.setenv("BIZINFO_PAGE_UNIT", "1")
    install_pages(monkeypatch, [
        Response(),
        Response(xml="<rss><channel><item><seq>one</seq><totCnt>2</totCnt></item></channel></rss>"),
        Response(xml="<rss><channel><totCnt>2</totCnt><item><seq>two</seq></item></channel></rss>"),
    ])
    assert len(client.fetch_support_programs()) == 2
    assert batch(client).status is CollectionStatus.SUCCESS
    assert batch(client).reported_count == 2


def test_page_cap_is_bounded_and_reported(client, monkeypatch):
    monkeypatch.setenv("BIZINFO_MAX_PAGES", "1")
    calls = install_pages(monkeypatch, [{"jsonArray": items(0, 100, 250)}])
    assert len(client.fetch_support_programs()) == 100
    assert len(calls) == 1 and batch(client).status is CollectionStatus.PARTIAL
    assert termination(client) == "page_cap"


def test_time_cap_is_bounded_and_reported(client, monkeypatch):
    clock = [0]
    monkeypatch.setattr("src.bizinfo_client.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("src.bizinfo_client.MAX_SECONDS", 120)
    def get(_session, _url, params, timeout):
        clock[0] = 121
        return Response({"jsonArray": items(0, 100, 250)})
    monkeypatch.setattr("src.bizinfo_client.requests.Session.get", get)
    assert len(client.fetch_support_programs()) == 100
    assert batch(client).status is CollectionStatus.PARTIAL
    assert termination(client) == "time_cap"


def test_missing_key_never_requests_and_replaces_previous_metadata(client, monkeypatch):
    install_pages(monkeypatch, [{"jsonArray": [], "totCnt": 0}])
    client.fetch_support_programs()
    client.support_key = None
    monkeypatch.setattr("src.bizinfo_client.requests.Session.get", lambda *a, **k: pytest.fail("no key"))
    assert client.fetch_support_programs() == []
    assert batch(client).status is CollectionStatus.FAILED
    assert termination(client) == "missing_key"


def test_legacy_list_client_does_not_invent_a_100_row_api_limit():
    result = _bizinfo_batch("bizinfo_support", items(0, 100))
    assert result.status is CollectionStatus.PARTIAL
    assert "메타데이터 없음" in result.note
    assert "상한" not in result.note and "반복" not in result.note


def test_malformed_json_envelope_is_not_empty_success(client, monkeypatch):
    calls = install_pages(monkeypatch, [{"error": "authentication rejected"}])
    assert client.fetch_support_programs() == []
    assert len(calls) == 1
    assert batch(client).status is CollectionStatus.FAILED
    assert termination(client) == "json_envelope"


@pytest.mark.parametrize("payload", [
    {"reqErr": "invalid query"},
    {"reqErr": {"message": "https://example.test/?crtfcKey=fake-private-key"}, "jsonArray": [], "totCnt": 0},
])
def test_api_reqerr_is_failed_even_if_empty_data_and_zero_total_are_present(client, monkeypatch, payload, caplog):
    calls = install_pages(monkeypatch, [payload])
    with caplog.at_level(logging.INFO):
        assert client.fetch_support_programs() == []
    assert len(calls) == 1
    assert batch(client).status is CollectionStatus.FAILED
    assert termination(client) == "api_error"
    assert "fake-private-key" not in caplog.text
