from src.normalizer import normalize_event, normalize_fanfandaero_support


def test_normalize_event_uses_stable_key_instead_of_view_count():
    item = {
        "nttNm": "중소기업 행사",
        "eventBeginEndDe": "2026-06-10 ~ 2026-06-11",
        "rceptPd": "2026-06-01 ~ 2026-06-05",
        "orginlUrlAdres": "https://example.com/event",
        "inqireCo": "123",
    }

    normalized = normalize_event(item)

    assert normalized["program_key"] != "event:123"
    assert normalized["program_key"].startswith("event:")
    assert normalized["url"] == "https://example.com/event"


def test_normalize_fanfandaero_support_maps_dates_and_source():
    item = {
        "sprtBizCd": "202616010200",
        "sprtBizNm": "온라인판로 종합지원사업",
        "rcritBgngYmd": "20260521",
        "rcritEndYmd": "20260605",
        "rcritEndChk": "N",
        "sprtBizTyNm": "유통·판로",
        "sprtBizTrgtNm": "중기업,소기업",
        "operInstNm": "한국중소벤처기업유통원",
        "txtDc": "온라인 판로 진출 지원",
        "hashtags": "서울,경기",
    }

    normalized = normalize_fanfandaero_support(item)

    assert normalized["program_key"] == "fanfandaero:202616010200"
    assert normalized["source"] == "fanfandaero"
    assert normalized["apply_start_at"] == "2026-05-21"
    assert normalized["apply_end_at"] == "2026-06-05"
    assert normalized["agency"] == "한국중소벤처기업유통원"
    assert normalized["summary_raw"] == "온라인 판로 진출 지원"
