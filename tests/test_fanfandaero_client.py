from src.fanfandaero_client import FanfandaeroClient


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_fanfandaero_fetches_paginated_support_programs(monkeypatch):
    monkeypatch.setenv("FANFANDAERO_PAGE_UNIT", "2")
    monkeypatch.setenv("FANFANDAERO_MAX_PAGES", "3")

    calls = []

    def fake_post(url, data, headers, timeout):
        calls.append((url, data["pageIndex"], data["pageUnit"], headers["Referer"]))
        if data["pageIndex"] == 1:
            return FakeResponse({
                "sprtBizApplListTotCnt": 3,
                "sprtBizApplList": [{"sprtBizCd": "A"}, {"sprtBizCd": "B"}],
            })
        return FakeResponse({
            "sprtBizApplListTotCnt": 3,
            "sprtBizApplList": [{"sprtBizCd": "C"}],
        })

    monkeypatch.setattr("src.fanfandaero_client.requests.post", fake_post)

    items = FanfandaeroClient().fetch_support_programs()

    assert [item["sprtBizCd"] for item in items] == ["A", "B", "C"]
    assert [call[1] for call in calls] == [1, 2]
    assert all(call[2] == 2 for call in calls)
    assert calls[0][3].endswith("/portal/v2/preSprtBizPbanc.do")


def test_fanfandaero_can_be_disabled(monkeypatch):
    monkeypatch.setenv("FANFANDAERO_ENABLED", "false")

    def fail_post(*args, **kwargs):
        raise AssertionError("disabled source should not call network")

    monkeypatch.setattr("src.fanfandaero_client.requests.post", fail_post)

    assert FanfandaeroClient().fetch_support_programs() == []
