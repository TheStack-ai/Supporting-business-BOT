"""Bounded, read-only pagination diagnostic. Never prints credentials or URLs."""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests


def inspect(spec):
    source, endpoint, key_name = spec
    key = os.environ.get(key_name)
    if not key:
        return {"source": source, "status": "key_unavailable"}
    reports, identifiers = [], []
    modes = [
        ("previous", {"searchCnt": 100, "pageIndex": 1}),
        ("page1", {"searchCnt": 0, "pageUnit": 100, "pageIndex": 1}),
        ("page2", {"searchCnt": 0, "pageUnit": 100, "pageIndex": 2}),
    ]
    for label, params in modes:
        try:
            response = requests.get(
                "https://www.bizinfo.go.kr/uss/rss/" + endpoint,
                params={"crtfcKey": key, "dataType": "json", **params}, timeout=(5, 20),
            )
            if response.status_code != 200:
                reports.append({"mode": label, "http_status": response.status_code})
                continue
            payload = response.json()
            rows = payload.get("jsonArray", []) if isinstance(payload, dict) else []
            rows = [rows] if isinstance(rows, dict) else rows
            rows = [row for row in rows if isinstance(row, dict)]
            identifiers.append({
                str(row.get("pblancId") or row.get("eventInfoId") or row.get("nttId") or row.get("eventId"))
                for row in rows
            })
            reports.append({
                "mode": label, "rows": len(rows),
                "root_fields": list(payload)[:12] if isinstance(payload, dict) else [],
                "root_total": payload.get("totCnt") if isinstance(payload, dict) else None,
                "row_total": rows[0].get("totCnt") if rows else None,
                "date_fields": {name: rows[0][name] for name in ("creatPnttm", "registDe", "regDate")
                                if rows and name in rows[0]},
            })
        except Exception as error:
            reports.append({"mode": label, "error_type": type(error).__name__})
    return {
        "source": source, "responses": reports,
        "page1_page2_overlap": len(identifiers[-1] & identifiers[-2]) if len(identifiers) == 3 else None,
        "sent": 0, "state_changed": False,
    }


def transport_probe():
    """Reproduce old single-attempt limits on the failed and boundary pages."""
    for source, endpoint, key_name, pages in (
        ("support", "bizinfoApi.do", "BIZINFO_SUPPORT_KEY", (1, 5, 13)),
        ("event", "bizinfoEventApi.do", "BIZINFO_EVENT_KEY", (1, 2, 3)),
    ):
        key = os.environ.get(key_name)
        if not key:
            print(json.dumps({"source": source, "error_type": "key_unavailable"})); continue
        for page in pages:
            started = time.monotonic()
            report = {"source": source, "page": page, "read_timeout": 10,
                      "attempt": 1, "sent": 0, "state_changed": False}
            try:
                response = requests.get("https://www.bizinfo.go.kr/uss/rss/" + endpoint,
                    params={"crtfcKey": key, "dataType": "json", "searchCnt": 0,
                            "pageUnit": 100, "pageIndex": page}, timeout=10)
                report["http_status"] = response.status_code
                response.raise_for_status()
                payload = response.json()
                rows = payload.get("jsonArray", [])
                rows = [rows] if isinstance(rows, dict) else rows
                report.update(rows=len(rows), total=rows[0].get("totCnt") if rows else payload.get("totCnt"),
                              api_error="reqErr" in payload)
            except requests.RequestException as error:
                report["error_type"] = type(error).__name__
            except (ValueError, TypeError, AttributeError, KeyError):
                report["error_type"] = "invalid_response"
            report["elapsed_seconds"] = round(time.monotonic() - started, 3)
            print(json.dumps(report), flush=True)


if __name__ == "__main__" and "--transport" in sys.argv:
    transport_probe()
elif __name__ == "__main__":
    with ThreadPoolExecutor(max_workers=2) as pool:
        for report in pool.map(inspect, [
            ("support", "bizinfoApi.do", "BIZINFO_SUPPORT_KEY"),
            ("event", "bizinfoEventApi.do", "BIZINFO_EVENT_KEY"),
        ]):
            print(json.dumps(report, ensure_ascii=False))
