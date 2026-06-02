import os
import requests
import json
import xmltodict
from typing import List, Dict, Any, Optional
import logging

logger = logging.getLogger(__name__)

SUPPORT_API_URL = "https://www.bizinfo.go.kr/uss/rss/bizinfoApi.do"
EVENT_API_URL = "https://www.bizinfo.go.kr/uss/rss/bizinfoEventApi.do"
DEFAULT_SEARCH_COUNT = 100
DEFAULT_MAX_PAGES = 5

class BizinfoClient:
    def __init__(self):
        self.support_key = os.getenv("BIZINFO_SUPPORT_KEY")
        self.event_key = os.getenv("BIZINFO_EVENT_KEY")

    def _search_count(self) -> int:
        try:
            return max(1, int(os.getenv("BIZINFO_SEARCH_COUNT", str(DEFAULT_SEARCH_COUNT))))
        except ValueError:
            return DEFAULT_SEARCH_COUNT

    def _max_pages(self) -> int:
        try:
            return max(1, int(os.getenv("BIZINFO_MAX_PAGES", str(DEFAULT_MAX_PAGES))))
        except ValueError:
            return DEFAULT_MAX_PAGES

    def _item_key(self, item: Dict[str, Any]) -> str:
        for key in ("pblancId", "eventInfoId", "eventId", "nttId", "inqireUrl", "pblancUrl", "orginlUrlAdres"):
            value = item.get(key)
            if value:
                return f"{key}:{value}"
        return json.dumps(item, ensure_ascii=False, sort_keys=True)

    def _json_items(self, response: requests.Response) -> Optional[List[Dict[str, Any]]]:
        try:
            data = response.json()
        except json.JSONDecodeError:
            return None

        items = data.get("jsonArray", [])
        if isinstance(items, dict):
            return [items]
        if isinstance(items, list):
            return items
        return []

    def _fetch_xml(self, url: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        xml_params = dict(params)
        xml_params.pop("dataType", None)
        try:
            response = requests.get(url, params=xml_params, timeout=10)
            response.raise_for_status()

            xml_data = xmltodict.parse(response.content)
            rss = xml_data.get('rss', {})
            channel = rss.get('channel', {})
            items = channel.get('item', [])

            if isinstance(items, dict):
                return [items]
            elif isinstance(items, list):
                return items
            else:
                return []

        except Exception as e:
            logger.error(f"Error fetching/parsing XML from {url}: {e}")
            return []

    def _fetch(self, url: str, api_key: str, params: Optional[Dict] = None) -> List[Dict[str, Any]]:
        if not api_key:
            logger.warning("API key not provided for %s", url)
            return []

        search_count = self._search_count()
        base_params = {
            "crtfcKey": api_key,
            "dataType": "json",
            "searchCnt": search_count,
        }
        if params:
            base_params.update(params)

        results: List[Dict[str, Any]] = []
        seen_keys: set[str] = set()
        max_pages = self._max_pages()

        for page_index in range(1, max_pages + 1):
            page_params = dict(base_params)
            page_params["pageIndex"] = page_index

            try:
                response = requests.get(url, params=page_params, timeout=10)
                response.raise_for_status()
                items = self._json_items(response)
            except Exception as e:
                logger.error(f"Error fetching JSON from {url}: {e}")
                items = None

            if items is None:
                if page_index == 1:
                    logger.info("JSON decode failed, attempting XML fallback for %s", url)
                    return self._fetch_xml(url, base_params)
                break

            if not items:
                if page_index == 1:
                    xml_items = self._fetch_xml(url, base_params)
                    if xml_items:
                        return xml_items
                break

            added_this_page = 0
            for item in items:
                if not isinstance(item, dict):
                    continue
                key = self._item_key(item)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                results.append(item)
                added_this_page += 1

            if added_this_page == 0 or len(items) < search_count:
                break

        return results

    def fetch_support_programs(self) -> List[Dict[str, Any]]:
        return self._fetch(SUPPORT_API_URL, self.support_key)

    def fetch_events(self) -> List[Dict[str, Any]]:
        return self._fetch(EVENT_API_URL, self.event_key)
