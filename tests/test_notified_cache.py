import json
import os
import tempfile
from datetime import datetime, timedelta
from src.notified_cache import load_notified_keys, save_notified_keys, filter_new_programs

def test_load_empty_when_no_file():
    keys = load_notified_keys("/tmp/nonexistent_cache_test_xyz.json")
    assert keys == set()

def test_save_and_load_roundtrip():
    path = tempfile.mktemp(suffix=".json")
    try:
        save_notified_keys({"support:1", "event:2"}, path)
        loaded = load_notified_keys(path)
        assert loaded == {"support:1", "event:2"}
    finally:
        if os.path.exists(path):
            os.unlink(path)

def test_filter_new_programs():
    programs = [
        {"program_key": "support:1", "title": "A"},
        {"program_key": "support:2", "title": "B"},
        {"program_key": "support:3", "title": "C"},
    ]
    notified = {"support:1", "support:3"}
    result = filter_new_programs(programs, notified)
    assert len(result) == 1
    assert result[0]["program_key"] == "support:2"

def test_save_prunes_old_entries():
    from src.notified_cache import PRUNE_DAYS
    path = tempfile.mktemp(suffix=".json")
    old_date = (datetime.now() - timedelta(days=PRUNE_DAYS + 10)).isoformat()
    recent_date = datetime.now().isoformat()
    data = {
        "entries": {
            "support:old": old_date,
            "support:new": recent_date,
        }
    }
    with open(path, "w") as f:
        json.dump(data, f)

    keys = load_notified_keys(path)
    keys.add("support:added")
    save_notified_keys(keys, path)

    with open(path) as f:
        saved = json.load(f)
    assert "support:old" not in saved["entries"]
    assert "support:new" in saved["entries"]
    assert "support:added" in saved["entries"]
    os.unlink(path)
