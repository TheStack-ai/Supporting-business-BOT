# src/decision_log.py
import json
import os
from datetime import datetime

DEFAULT_LOG_PATH = "data/decisions.jsonl"


def log_decision(
    program: dict,
    grade: str,
    reason: str,
    stage: str,
    log_path: str = DEFAULT_LOG_PATH,
) -> None:
    entry = {
        "ts": datetime.now().isoformat(),
        "key": program.get("program_key", ""),
        "title": program.get("title", ""),
        "grade": grade,
        "reason": reason,
        "stage": stage,
    }
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
