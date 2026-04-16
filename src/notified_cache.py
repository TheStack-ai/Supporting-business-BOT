import json
import os
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

DEFAULT_CACHE_PATH = "data/notified_keys.json"
PRUNE_DAYS = 90


def load_notified_keys(path: str = DEFAULT_CACHE_PATH) -> set[str]:
    if not os.path.exists(path):
        return set()
    try:
        with open(path) as f:
            data = json.load(f)
        return set(data.get("entries", {}).keys())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to load notified cache: {e}")
        return set()


def save_notified_keys(keys: set[str], path: str = DEFAULT_CACHE_PATH) -> None:
    existing_entries = {}
    if os.path.exists(path):
        try:
            with open(path) as f:
                data = json.load(f)
            existing_entries = data.get("entries", {})
        except (json.JSONDecodeError, OSError):
            pass

    now = datetime.now()
    cutoff = now - timedelta(days=PRUNE_DAYS)

    merged = {}
    for key in keys:
        if key in existing_entries:
            ts = existing_entries[key]
            try:
                dt = datetime.fromisoformat(ts)
                if dt >= cutoff:
                    merged[key] = ts
            except (ValueError, TypeError):
                merged[key] = now.isoformat()
        else:
            merged[key] = now.isoformat()

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump({"entries": merged, "updated_at": now.isoformat()}, f, ensure_ascii=False)


def filter_new_programs(programs: list[dict], notified: set[str]) -> list[dict]:
    return [p for p in programs if p.get("program_key") not in notified]
