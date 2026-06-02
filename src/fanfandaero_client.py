import logging
import os
from typing import Any, Dict, List

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://fanfandaero.kr"
LIST_URL = f"{BASE_URL}/portal/v2/selectSprtBizPbancList.do"
REFERER_URL = f"{BASE_URL}/portal/v2/preSprtBizPbanc.do"
DEFAULT_PAGE_UNIT = 100
DEFAULT_MAX_PAGES = 5


class FanfandaeroClient:
    def __init__(self):
        self.enabled = os.getenv("FANFANDAERO_ENABLED", "true").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }

    def _page_unit(self) -> int:
        try:
            return max(1, int(os.getenv("FANFANDAERO_PAGE_UNIT", str(DEFAULT_PAGE_UNIT))))
        except ValueError:
            return DEFAULT_PAGE_UNIT

    def _max_pages(self) -> int:
        try:
            return max(1, int(os.getenv("FANFANDAERO_MAX_PAGES", str(DEFAULT_MAX_PAGES))))
        except ValueError:
            return DEFAULT_MAX_PAGES

    def _fetch_page(self, page_index: int, page_unit: int) -> tuple[list[dict[str, Any]], int]:
        params = {
            "brno": "",
            "pageIndex": page_index,
            "pageUnit": page_unit,
            "searchTypeStr": "",
            "searchTargetStr": "",
            "searchAreaStr": "",
            "searchText": "",
            "noSearchSprt": "",
            "searchOrder": "1",
            "sortOrder": "",
            "testLoginId": "",
            "notSearchSprtBizCd": "",
        }
        headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Referer": REFERER_URL,
        }

        response = requests.post(LIST_URL, data=params, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()

        items = data.get("sprtBizApplList") or []
        if not isinstance(items, list):
            items = []

        total = data.get("sprtBizApplListTotCnt") or 0
        try:
            total = int(total)
        except (TypeError, ValueError):
            total = 0

        return [item for item in items if isinstance(item, dict)], total

    def fetch_support_programs(self) -> List[Dict[str, Any]]:
        if not self.enabled:
            logger.info("Fanfandaero source disabled")
            return []

        page_unit = self._page_unit()
        max_pages = self._max_pages()
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        total = 0

        for page_index in range(1, max_pages + 1):
            try:
                items, total = self._fetch_page(page_index, page_unit)
            except Exception as e:
                logger.error("Error fetching Fanfandaero page %s: %s", page_index, e)
                break

            if not items:
                break

            added_this_page = 0
            for item in items:
                key = str(item.get("sprtBizCd") or item.get("url") or item.get("sprtBizNm") or "")
                if not key or key in seen:
                    continue
                seen.add(key)
                results.append(item)
                added_this_page += 1

            if added_this_page == 0 or len(items) < page_unit or (total and len(results) >= total):
                break

        logger.info("Fanfandaero: fetched %s items (total=%s)", len(results), total)
        return results
