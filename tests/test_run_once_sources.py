from src.run_once import _apply_hard_filter, _ingest_all


class FakeBizinfoClient:
    def fetch_support_programs(self):
        return [{
            "pblancId": "B1",
            "pblancNm": "기업마당 지원사업",
            "pblancSumry": "요약",
        }]

    def fetch_events(self):
        return [{
            "eventInfoId": "E1",
            "nttNm": "기업마당 행사",
            "nttCn": "행사 내용",
        }]


class FakeFanfandaeroClient:
    def fetch_support_programs(self):
        return [{
            "sprtBizCd": "F1",
            "sprtBizNm": "판판대로 지원사업",
            "txtDc": "판로 지원",
        }]


def test_ingest_all_includes_fanfandaero(monkeypatch):
    saved = []
    logs = []

    monkeypatch.setattr("src.run_once.upsert_program", lambda item: saved.append(item))
    monkeypatch.setattr("src.run_once.log_ingestion_run", lambda item: logs.append(item))

    items = _ingest_all(FakeBizinfoClient(), FakeFanfandaeroClient())

    assert [item["source"] for item in items] == ["bizinfo", "bizinfo", "fanfandaero"]
    assert [item["source"] for item in saved] == ["bizinfo", "bizinfo", "fanfandaero"]
    assert [item["kind"] for item in logs] == ["support", "event", "fanfandaero_support"]


def test_hard_filter_splits_clear_noise_from_candidates():
    items = [
        {"title": "TV홈쇼핑 입점지원", "summary_raw": "온라인쇼핑몰"},
        {"title": "MRO 공공조달 컨설팅", "summary_raw": "입찰 성공률 제고"},
    ]

    candidates, rejected = _apply_hard_filter(items)

    assert [item["title"] for item in candidates] == ["MRO 공공조달 컨설팅"]
    assert rejected[0][0]["title"] == "TV홈쇼핑 입점지원"
