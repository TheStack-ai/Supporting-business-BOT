import json
import logging

import pytest
import requests

import src.bizinfo_client as module
from src.source_result import CollectionStatus


class Response:
    def __init__(self, ids=(), total=0, status=200, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self.ids, self.total = ids, total

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError('https://example.test/?crtfcKey=private', response=self)

    def json(self):
        return {'jsonArray': [{'pblancId': str(i), 'totCnt': self.total} for i in self.ids], 'totCnt': self.total}


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv('BIZINFO_SUPPORT_KEY', 'private')
    monkeypatch.setenv('BIZINFO_PAGE_UNIT', '2')
    monkeypatch.setenv('BIZINFO_MAX_PAGES', '10')
    clock, calls, waits = [0.0], [], []
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    def sleep(seconds):
        waits.append(seconds)
        clock[0] += seconds
    monkeypatch.setattr(module.time, 'sleep', sleep)
    client = module.BizinfoClient()
    def install(results):
        results = iter(results)
        def get(url, params, timeout):
            calls.append((params.copy(), timeout, clock[0]))
            result = next(results)
            clock[0] += 0.2
            if isinstance(result, Exception):
                raise result
            return result
        monkeypatch.setattr(client._session, 'get', get)
    return client, install, calls, waits, clock


def meta(client):
    return client.collection_metadata['bizinfo_support']


def test_timeout_retries_same_page_without_dropping_or_duplicating_items(setup, caplog):
    client, install, calls, waits, _clock = setup
    install([Response([1, 2], 5), requests.ReadTimeout('crtfcKey=private'),
             Response([3, 4], 5), Response([5], 5)])
    with caplog.at_level(logging.INFO):
        rows = client.fetch_support_programs()
    assert [row['pblancId'] for row in rows] == ['1', '2', '3', '4', '5']
    assert [call[0]['pageIndex'] for call in calls] == [1, 2, 2, 3]
    assert all(timeout == (5, 30) for _params, timeout, _time in calls)
    assert waits == [1]
    assert meta(client)['status'] == 'success'
    assert meta(client)['retry_count'] == 1
    assert meta(client)['failed_attempt_count'] == 1
    assert meta(client)['attempts'][1]['error_kind'] == 'read_timeout'
    assert 'private' not in caplog.text and 'crtfcKey' not in json.dumps(meta(client))


def test_recoverable_http_and_connection_failures_are_retried(setup):
    client, install, calls, waits, _clock = setup
    install([Response(status=503), requests.ConnectionError('secret'), Response([1], 1)])
    assert len(client.fetch_support_programs()) == 1
    assert len(calls) == 3 and waits == [1, 2]
    assert meta(client)['attempts'][0]['http_status'] == 503
    assert meta(client)['status'] == 'success'


def test_rate_limit_honors_retry_after(setup):
    client, install, calls, waits, _clock = setup
    install([Response(status=429, headers={'Retry-After': '12'}), Response([1], 1)])
    client.fetch_support_programs()
    assert waits == [12]
    assert calls[1][2] - calls[0][2] >= 12
    assert meta(client)['status'] == 'success'


@pytest.mark.parametrize('failure', [Response(status=401), Response(status=403),
                                   requests.exceptions.SSLError('crtfcKey=private')])
def test_permanent_errors_do_not_retry_or_disable_tls(setup, failure):
    client, install, calls, waits, _clock = setup
    install([failure])
    assert client.fetch_support_programs() == []
    assert len(calls) == 1 and not waits
    assert meta(client)['status'] == 'failed'


def test_exhausted_page_is_partial_with_exact_failed_page(setup):
    client, install, calls, waits, _clock = setup
    install([Response([1, 2], 4), *[requests.ConnectTimeout('secret') for _ in range(4)]])
    assert len(client.fetch_support_programs()) == 2
    assert [call[0]['pageIndex'] for call in calls] == [1, 2, 2, 2, 2]
    assert waits == [1, 2, 4]
    batch = client.collection_batches['bizinfo_support']
    assert batch.status is CollectionStatus.PARTIAL
    assert batch.reported_count == 4
    assert '2페이지 연결 시간 초과' in batch.note
    assert '4회' in batch.note


def test_retry_after_exceeding_budget_never_retries_early(setup):
    client, install, calls, waits, _clock = setup
    install([Response(status=429, headers={'Retry-After': '600'})])
    client.fetch_support_programs()
    assert len(calls) == 1 and not waits
    assert meta(client)['termination'] == 'time_cap'
    assert meta(client)['attempts'][0]['retry_stopped'] == 'insufficient_time_budget'


def test_failed_support_does_not_contaminate_event_collection(setup, monkeypatch):
    client, install, calls, _waits, _clock = setup
    monkeypatch.setattr(client, 'event_key', 'event-private')
    install([Response(status=403), Response([1], 1)])
    client.fetch_support_programs()
    assert len(client.fetch_events()) == 1
    assert client.collection_metadata['bizinfo_event']['status'] == 'success'
    assert client.collection_metadata['bizinfo_event']['failed_attempt_count'] == 0
    assert meta(client)['status'] == 'failed'
