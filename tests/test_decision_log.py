import json
import os
import tempfile
from src.decision_log import log_decision

def test_log_decision_creates_jsonl():
    path = tempfile.mktemp(suffix=".jsonl")
    try:
        log_decision(
            program={"program_key": "support:123", "title": "테스트 공고"},
            grade="A",
            reason="자격 충족",
            stage="stage2",
            log_path=path,
        )
        with open(path) as f:
            line = f.readline()
        entry = json.loads(line)
        assert entry["key"] == "support:123"
        assert entry["grade"] == "A"
        assert entry["reason"] == "자격 충족"
        assert entry["stage"] == "stage2"
        assert "ts" in entry
    finally:
        if os.path.exists(path):
            os.unlink(path)

def test_log_decision_appends():
    path = tempfile.mktemp(suffix=".jsonl")
    try:
        log_decision({"program_key": "s:1", "title": "A"}, "A", "ok", "stage2", path)
        log_decision({"program_key": "s:2", "title": "B"}, "C", "no", "stage2", path)
        with open(path) as f:
            lines = f.readlines()
        assert len(lines) == 2
    finally:
        if os.path.exists(path):
            os.unlink(path)
