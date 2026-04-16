# src/llm_filter.py
import os
import json
import logging
from dataclasses import dataclass

from src.company_profile import build_stage1_prompt, build_stage2_prompt

logger = logging.getLogger(__name__)

BATCH_SIZE = 10


@dataclass
class Assessment:
    grade: str       # "A", "B", "C"
    reason: str      # One-line rationale
    eligibility: str  # "충족", "미확인", "미충족"


def parse_stage1_response(raw, total_count: int) -> dict[int, str]:
    """Parse Stage 1 JSON response. Missing/invalid entries default to PASS."""
    result = {}
    try:
        if isinstance(raw, str):
            raw = json.loads(raw)
        items = raw.get("results", [])
        for item in items:
            result[item["id"]] = item.get("decision", "PASS")
    except Exception:
        pass

    # Fill missing IDs with PASS
    for i in range(1, total_count + 1):
        if i not in result:
            result[i] = "PASS"

    return result


def parse_stage2_response(raw) -> Assessment:
    """Parse Stage 2 JSON response. Invalid values get safe defaults."""
    try:
        if isinstance(raw, str):
            raw = json.loads(raw)
        grade = raw.get("grade", "B")
        if grade not in ("A", "B", "C"):
            grade = "B"
        reason = raw.get("reason", "LLM 판단 불가")
        eligibility = raw.get("eligibility", "미확인")
        if eligibility not in ("충족", "미확인", "미충족"):
            eligibility = "미확인"
        return Assessment(grade=grade, reason=reason, eligibility=eligibility)
    except Exception:
        return Assessment(grade="B", reason="LLM 판단 불가", eligibility="미확인")


def _get_gemini_client():
    """Lazy-init Gemini client. Returns None if key not set."""
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return None
    from google import genai
    return genai.Client(api_key=api_key)


def stage1_quick_filter(programs: list[dict]) -> list[dict]:
    """Batch-filter programs by title+summary. Returns PASS-ed programs."""
    client = _get_gemini_client()
    if client is None:
        raise RuntimeError("GEMINI_API_KEY not configured")

    passed = []
    for batch_start in range(0, len(programs), BATCH_SIZE):
        batch = programs[batch_start : batch_start + BATCH_SIZE]
        prompt = build_stage1_prompt(batch)

        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config={
                    "response_mime_type": "application/json",
                    "response_schema": {
                        "type": "object",
                        "properties": {
                            "results": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "id": {"type": "integer"},
                                        "decision": {"type": "string", "enum": ["PASS", "REJECT"]},
                                    },
                                    "required": ["id", "decision"],
                                },
                            }
                        },
                    },
                },
            )
            decisions = parse_stage1_response(json.loads(response.text), len(batch))
        except Exception as e:
            logger.error(f"Stage 1 Gemini call failed: {e}")
            decisions = {i: "PASS" for i in range(1, len(batch) + 1)}

        for i, program in enumerate(batch, 1):
            if decisions.get(i) == "PASS":
                passed.append(program)

    return passed


def stage2_assess(program: dict, detail_text: str) -> Assessment:
    """Assess single program with detail page text."""
    if not detail_text:
        return Assessment(grade="B", reason="상세페이지 접근 불가", eligibility="미확인")

    client = _get_gemini_client()
    if client is None:
        raise RuntimeError("GEMINI_API_KEY not configured")

    prompt = build_stage2_prompt(program, detail_text)

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config={
                "response_mime_type": "application/json",
                "response_schema": {
                    "type": "object",
                    "properties": {
                        "grade": {"type": "string", "enum": ["A", "B", "C"]},
                        "reason": {"type": "string"},
                        "eligibility": {"type": "string", "enum": ["충족", "미확인", "미충족"]},
                    },
                    "required": ["grade", "reason", "eligibility"],
                },
            },
        )
        return parse_stage2_response(json.loads(response.text))
    except Exception as e:
        logger.error(f"Stage 2 Gemini call failed: {e}")
        return Assessment(grade="B", reason="LLM 판단 불가", eligibility="미확인")
