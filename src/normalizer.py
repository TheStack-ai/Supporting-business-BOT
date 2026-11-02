from datetime import datetime
import hashlib
import html
import re
from .due_parser import parse_period
from typing import Any, Dict, Literal, assert_never


def _stable_seq(*parts: Any) -> str:
    raw = "|".join(str(part or "") for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _format_yyyymmdd(value: Any) -> str | None:
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return text or None

def normalize_support(item: Dict[str, Any]) -> Dict[str, Any]:
    # Item keys based on Bizinfo JSON response (may vary, need to be robust)
    # Common keys: pblancId, pblancNm, reqstBeginEndDe, reqstDt, creatPnttm, etc.
    # We map them to our schema.
    
    seq = item.get('pblancId', '')
    title = item.get('pblancNm', '')
    
    # Periods
    # 'reqstBeginEndDe' usually contains "YYYY-MM-DD ~ YYYY-MM-DD"
    apply_period_raw = item.get('reqstBeginEndDe') or item.get('reqstDt')
    start_at, end_at = parse_period(apply_period_raw)
    
    return {
        "program_key": f"support:{seq}",
        "kind": "support",
        "source": "bizinfo",
        "seq": seq,
        "title": title,
        "summary_raw": item.get('pblancSumry') or item.get('pblancCn') or item.get('bsnsSumryCn'), # Added bsnsSumryCn
        "agency": item.get('jrsdinstNm') or item.get('excInsttNm'),
        "category_l1": item.get('pblancClCd'),
        "region_raw": item.get('jrsdinstNm'),
        "apply_period_raw": apply_period_raw,
        "apply_start_at": start_at,
        "apply_end_at": end_at,
        "url": item.get('pblancUrl') or item.get('inqireUrl') or f"https://www.bizinfo.go.kr/web/ext/retrieveDtlNews.do?pblancId={seq}", # Added pblancUrl
        "created_at_source": item.get('creatPnttm'),
        "updated_at_source": None,
        "ingested_at": datetime.now().isoformat()
    }

def normalize_event(item: Dict[str, Any]) -> Dict[str, Any]:
    # Try multiple keys for ID and Title
    # Log says: nttNm (Title), nttCn (Content), registDe (Date), eventBeginEndDe (Period), orginlUrlAdres (URL)
    # Also eventInfoId might be ID? But log shows 'eventInfoId': '...' isn't in top content?
    # Wait, 'eventInfoId' IS in keys list.
    title = item.get('nttNm') or item.get('eventNm') or item.get('pblancNm') or item.get('title', '')
    url = item.get('orginlUrlAdres') or item.get('inqireUrl') or item.get('url')
    seq = item.get('eventInfoId') or item.get('eventId') or item.get('pblancId')
    if not seq:
        seq = _stable_seq(title, item.get('eventBeginEndDe'), item.get('rceptPd'), url)
    
    # Periods
    apply_period_raw = item.get('rceptPd') or item.get('reqstBeginEndDe')
    apply_start, apply_end = parse_period(apply_period_raw)
    
    event_period_raw = item.get('eventBeginEndDe') or item.get('eventPeriod')
    event_start, event_end = parse_period(event_period_raw)
    
    return {
        "program_key": f"event:{seq}",
        "kind": "event",
        "source": "bizinfo",
        "seq": seq,
        "title": title,
        "summary_raw": item.get('nttCn') or item.get('eventCn') or item.get('pblancCn'),
        "agency": item.get('insttNm') or item.get('jrsdinstNm'),
        "category_l1": "행사",
        "region_raw": item.get('areaNm') or item.get('jrsdinstNm'),
        "apply_period_raw": apply_period_raw,
        "apply_start_at": apply_start,
        "apply_end_at": apply_end,
        "event_period_raw": event_period_raw,
        "event_start_at": event_start,
        "event_end_at": event_end,
        "url": url or f"https://www.bizinfo.go.kr/web/ext/retrieveDtlNews.do?pblancId={seq}",
        "created_at_source": item.get('registDe') or item.get('regDate') or item.get('creatPnttm'),
        "updated_at_source": None,
        "ingested_at": datetime.now().isoformat()
    }


def normalize_fanfandaero_support(item: Dict[str, Any]) -> Dict[str, Any]:
    seq = str(item.get('sprtBizCd') or '').strip()
    title = item.get('sprtBizNm') or ''
    if not seq:
        seq = _stable_seq(title, item.get('rcritBgngYmd'), item.get('rcritEndYmd'), item.get('url'))

    start_at = _format_yyyymmdd(item.get('rcritBgngYmd'))
    end_at = None if item.get('rcritEndChk') == 'Y' else _format_yyyymmdd(item.get('rcritEndYmd'))
    if start_at and end_at:
        apply_period_raw = f"{start_at} ~ {end_at}"
    elif start_at and item.get('rcritEndChk') == 'Y':
        apply_period_raw = f"{start_at} ~ 예산소진시까지"
    else:
        apply_period_raw = None

    url = item.get('url') or f"https://fanfandaero.kr/portal/v2/preSprtBizPbancDetail.do?sprtBizCd={seq}"
    agency = item.get('operInstNm') or item.get('jrsdInsttNm') or "한국중소벤처기업유통원"
    summary = item.get('txtDc') or item.get('cn') or item.get('sprtBizCg3Nm') or item.get('hashtags')

    return {
        "program_key": f"fanfandaero:{seq}",
        "kind": "support",
        "source": "fanfandaero",
        "seq": seq,
        "title": title,
        "summary_raw": summary,
        "agency": agency,
        "category_l1": item.get('sprtBizTyNm') or item.get('sprtBizCg3Nm'),
        "region_raw": item.get('hashtags') or item.get('sprtBizCtpvNm'),
        "apply_period_raw": apply_period_raw,
        "apply_start_at": start_at,
        "apply_end_at": end_at,
        "url": url,
        "created_at_source": item.get('batchPnttm'),
        "updated_at_source": None,
        "ingested_at": datetime.now().isoformat()
    }


def _sbiz_period(item: Dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    receipt_period = item.get("rcptPd")
    if isinstance(receipt_period, dict):
        start_at = _format_yyyymmdd(receipt_period.get("from"))
        end_at = _format_yyyymmdd(receipt_period.get("to"))
        raw = f"{start_at or ''} ~ {end_at or ''}".strip()
        return start_at, end_at, raw

    raw = str(item.get("aplyPd") or "").strip()
    start_at, end_at = parse_period(raw)
    if start_at is None:
        start_at = _format_yyyymmdd(item.get("aplyPdBgngYmd"))
    if end_at is None:
        end_at = _format_yyyymmdd(item.get("aplyPdEndYmd"))
    return start_at, end_at, raw or None


def _sbiz_status(item: Dict[str, Any]) -> str:
    status = str(item.get("aplyPsbltySe") or "").strip()
    if status in {"Y", "신청가능"}:
        return "접수중"
    if status in {"N", "EX", "신청불가", "신청마감", "마감"}:
        return "마감"
    if "상시" in status:
        return "상시"
    if "예산" in status or "소진" in status:
        return "예산소진"
    if "예정" in status:
        return "회차예정"
    return "불명"


def _plain_text(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(part) for part in value if part)
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def normalize_sbiz24_support(
    item: Dict[str, Any],
    source: Literal["sbiz24", "sbiz24_combine"],
) -> Dict[str, Any]:
    match source:
        case "sbiz24":
            seq = str(item.get("pbancSn") or item.get("pbancId") or "").strip()
            program_key = f"sbiz24:{seq}"
            agency = "소상공인시장진흥공단"
            url = f"https://www.sbiz24.kr/#/pbanc/{seq}"
        case "sbiz24_combine":
            seq = str(item.get("pbancId") or item.get("pbancSn") or "").strip()
            program_key = f"support:{seq}" if seq.startswith("PBLN") else f"sbiz24_combine:{seq}"
            agency = item.get("departNm") or item.get("jrsdInstNm") or "기관 미상"
            route = "extldPbanc" if seq.startswith("PBLN") else "pbanc"
            url = f"https://www.sbiz24.kr/#/{route}/{seq}"
        case unreachable:
            assert_never(unreachable)

    start_at, end_at, period_raw = _sbiz_period(item)
    summary_parts = [
        _plain_text(item.get(field))
        for field in ("bizPrps", "sprtTrgtNm", "rcrtTypeCdNm", "pbancDtlCn", "hstgNm")
    ]
    summary = " | ".join(dict.fromkeys(part for part in summary_parts if part))[:4000]

    return {
        "program_key": program_key,
        "kind": "support",
        "source": source,
        "seq": seq,
        "title": item.get("pbancNm") or "",
        "summary_raw": summary,
        "agency": agency,
        "category_l1": item.get("rcrtTypeCdNm") or item.get("bizType"),
        "region_raw": _plain_text(item.get("regionNmList") or item.get("regionNm") or item.get("pbancRgn")),
        "status": _sbiz_status(item),
        "apply_period_raw": period_raw,
        "apply_start_at": start_at,
        "apply_end_at": end_at,
        "url": item.get("bizAplySiteUrlAddr") or url,
        "created_at_source": item.get("regDt"),
        "updated_at_source": item.get("mdfcnDt"),
        "ingested_at": datetime.now().isoformat(),
    }
