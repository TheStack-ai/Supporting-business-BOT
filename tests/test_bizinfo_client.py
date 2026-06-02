from src.bizinfo_client import BizinfoClient


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.content = b""

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_bizinfo_fetch_paginates_until_empty(monkeypatch):
    monkeypatch.setenv("BIZINFO_SUPPORT_KEY", "key")
    monkeypatch.setenv("BIZINFO_SEARCH_COUNT", "1")
    monkeypatch.setenv("BIZINFO_MAX_PAGES", "3")

    calls = []

    def fake_get(url, params, timeout):
        calls.append(params["pageIndex"])
        if params["pageIndex"] == 1:
            return FakeResponse({"jsonArray": [{"pblancId": "1"}]})
        if params["pageIndex"] == 2:
            return FakeResponse({"jsonArray": [{"pblancId": "2"}]})
        return FakeResponse({"jsonArray": []})

    monkeypatch.setattr("src.bizinfo_client.requests.get", fake_get)

    items = BizinfoClient().fetch_support_programs()

    assert [item["pblancId"] for item in items] == ["1", "2"]
    assert calls == [1, 2, 3]


def test_bizinfo_fetch_stops_when_page_repeats(monkeypatch):
    monkeypatch.setenv("BIZINFO_SUPPORT_KEY", "key")
    monkeypatch.setenv("BIZINFO_SEARCH_COUNT", "2")
    monkeypatch.setenv("BIZINFO_MAX_PAGES", "5")

    calls = []

    def fake_get(url, params, timeout):
        calls.append(params["pageIndex"])
        return FakeResponse({"jsonArray": [{"pblancId": "1"}, {"pblancId": "2"}]})

    monkeypatch.setattr("src.bizinfo_client.requests.get", fake_get)

    items = BizinfoClient().fetch_support_programs()

    assert [item["pblancId"] for item in items] == ["1", "2"]
    assert calls == [1, 2]
