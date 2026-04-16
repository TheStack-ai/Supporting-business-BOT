import re
import time
import logging
import requests

logger = logging.getLogger(__name__)

MAX_TEXT_LENGTH = 8000
REQUEST_DELAY = 1.0
REQUEST_TIMEOUT = 10

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9",
}

_last_request_time = 0.0


def extract_text_from_html(html: str | None) -> str:
    if not html:
        return ""

    text = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&nbsp;", " ").replace("&quot;", '"')
    text = re.sub(r"\s+", " ", text).strip()

    if len(text) > MAX_TEXT_LENGTH:
        text = text[:MAX_TEXT_LENGTH]

    return text


def fetch_detail(url: str) -> str:
    global _last_request_time

    if not url or not url.startswith("http"):
        return ""

    elapsed = time.time() - _last_request_time
    if elapsed < REQUEST_DELAY:
        time.sleep(REQUEST_DELAY - elapsed)

    try:
        resp = requests.get(url, headers=_HEADERS, timeout=REQUEST_TIMEOUT)
        _last_request_time = time.time()
        resp.raise_for_status()
        return extract_text_from_html(resp.text)
    except Exception as e:
        logger.warning(f"Failed to fetch detail page {url}: {e}")
        return ""
