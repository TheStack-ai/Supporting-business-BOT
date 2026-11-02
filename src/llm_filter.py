"""Evidence-led assessment. Incomplete evaluation is held, not advertised as B."""
import os
import json
import logging
from dataclasses import dataclass, replace
from enum import StrEnum
import re

from google.genai import errors as genai_errors

from src.company_profile import build_stage1_prompt, build_stage2_prompt
from src.company_fit import assess_company_fit, company_fit_status

logger = logging.getLogger(__name__)
BATCH_SIZE = 10


class EligibilityVerdict(StrEnum):
    CONFIRMED = "확인됨"
    CONDITIONAL = "조건부"
    NEEDS_CONFIRMATION = "확인 필요"
    INELIGIBLE = "신청 불가"
    PIVOT_CANDIDATE = "사업전환 후보"


class GeminiConfigurationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Assessment:
    grade: str  # A/B/C, or REVIEW for an incomplete assessment (never sent)
    reason: str
    eligibility: str
    verdict: EligibilityVerdict = EligibilityVerdict.NEEDS_CONFIRMATION
    review_pending: bool = False
    fit_reason: str = ""
    missing_requirements: tuple[str, ...] = ()
    evidence_quotes: tuple[str, ...] = ()


class Stage1Result(list):
    """List-compatible successful candidates plus explicit retryable omissions."""
    def __init__(self):
        super().__init__()
        self.pending_keys: set[str] = set()
        self.rejected_keys: set[str] = set()


def pending_assessment(reason: str, *, fit_reason: str = "", checks=()) -> Assessment:
    return Assessment("REVIEW", reason, "미확인", review_pending=True,
                      fit_reason=fit_reason, missing_requirements=tuple(checks))


def _strings(value) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(dict.fromkeys(x.strip() for x in value if isinstance(x, str) and x.strip()))


def parse_stage1_response(raw, total_count: int) -> dict[int, str]:
    """Invalid/missing results are REVIEW, not a positive recommendation."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = {}
    items = raw.get("results", []) if isinstance(raw, dict) else []
    result = {i: "REVIEW" for i in range(1, total_count + 1)}
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict) or type(item.get("id")) is not int:
                continue
            index, decision = item["id"], item.get("decision")
            if index in result:
                result[index] = decision if decision in ("PASS", "REJECT", "REVIEW") else "REVIEW"
    return result


def parse_stage2_response(raw) -> Assessment:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    grade, reason, eligibility = raw.get("grade"), raw.get("reason"), raw.get("eligibility")
    if (grade not in ("A", "B", "C", "REVIEW") or not isinstance(reason, str)
            or not reason.strip() or eligibility not in ("충족", "미확인", "미충족")):
        return pending_assessment("평가 응답을 확인하지 못함")
    try:
        verdict = EligibilityVerdict(raw.get("verdict"))
    except (TypeError, ValueError):
        verdict = (EligibilityVerdict.INELIGIBLE if eligibility == "미충족"
                   else EligibilityVerdict.NEEDS_CONFIRMATION)
    checks = _strings(raw.get("missing_requirements"))
    if eligibility == "미충족" or verdict in (EligibilityVerdict.INELIGIBLE, EligibilityVerdict.PIVOT_CANDIDATE):
        grade = "C"
    elif grade == "A" and (eligibility != "충족" or verdict is not EligibilityVerdict.CONFIRMED or checks):
        grade = "B"
    return Assessment(grade, reason.strip(), eligibility, verdict,
                      review_pending=grade == "REVIEW",
                      fit_reason=str(raw.get("fit_reason") or "").strip(),
                      missing_requirements=checks, evidence_quotes=_strings(raw.get("evidence_quotes")))


def _get_gemini_client():
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return None
    from google import genai
    return genai.Client(api_key=api_key)


def stage1_quick_filter(programs: list[dict]) -> Stage1Result:
    result = Stage1Result()
    candidates = []
    for program in programs:
        key = str(program.get("program_key") or "")
        status = company_fit_status(program)
        if status == "reject":
            result.rejected_keys.add(key)
        elif status == "pending":
            result.pending_keys.add(key)
        else:
            candidates.append(program)
    if not candidates:
        return result
    try:
        client = _get_gemini_client()
    except (ValueError, RuntimeError):
        client = None
    if client is None:
        result.pending_keys.update(str(p.get("program_key") or "") for p in candidates)
        return result

    for start in range(0, len(candidates), BATCH_SIZE):
        batch = candidates[start:start + BATCH_SIZE]
        try:
            response = client.models.generate_content(
                model="gemini-3.1-flash-lite-preview", contents=build_stage1_prompt(batch),
                config={"response_mime_type": "application/json", "response_schema": {
                    "type": "object", "properties": {"results": {"type": "array", "items": {
                        "type": "object", "properties": {"id": {"type": "integer"},
                        "decision": {"type": "string", "enum": ["PASS", "REJECT", "REVIEW"]}},
                        "required": ["id", "decision"]}}}, "required": ["results"]}},
            )
            decisions = parse_stage1_response(response.text, len(batch))
        except (AttributeError, TypeError, ValueError, RuntimeError, genai_errors.APIError) as error:
            logger.warning("Stage 1 assessment unavailable: %s", type(error).__name__)
            decisions = {i: "REVIEW" for i in range(1, len(batch) + 1)}
        for index, program in enumerate(batch, 1):
            key = str(program.get("program_key") or "")
            if decisions[index] == "PASS":
                result.append(program)
            elif decisions[index] == "REJECT":
                result.rejected_keys.add(key)
            else:
                result.pending_keys.add(key)
    return result


def apply_assessment_policy(program: dict, assessment: Assessment, detail_text: str) -> Assessment:
    fit = assess_company_fit(program, detail_text)
    if not fit.relevant:
        return Assessment("C", fit.reject_reason or fit.reason, "미확인",
                          EligibilityVerdict.INELIGIBLE if fit.reject_reason else EligibilityVerdict.NEEDS_CONFIRMATION)
    checks = tuple(dict.fromkeys((*fit.missing_requirements, *assessment.missing_requirements)))
    if fit.review_requirements:
        return pending_assessment("핵심 신청자격에 대한 회사 근거 확인 필요", fit_reason=fit.reason, checks=checks)
    # A person's name cannot establish gender/female-enterprise qualification.
    if re.search(r"여성\s*(?:대표|기업|CEO)|대표.{0,15}여성", assessment.reason, re.I):
        return pending_assessment("여성대표·여성기업 자격을 추정할 수 없음", fit_reason=fit.reason,
                                  checks=("여성대표·여성기업 자격 확인",))
    if assessment.review_pending or assessment.grade == "REVIEW":
        return replace(assessment, fit_reason=fit.reason, missing_requirements=checks)
    if assessment.eligibility == "미충족" or assessment.verdict in (EligibilityVerdict.INELIGIBLE, EligibilityVerdict.PIVOT_CANDIDATE):
        return replace(assessment, grade="C", fit_reason=fit.reason)
    if assessment.grade == "C":
        return assessment
    source = re.sub(r"\s+", " ", str(detail_text) + " " + str(program.get("title") or "") + " " + str(program.get("summary_raw") or ""))
    evidence = tuple(quote for quote in assessment.evidence_quotes
                     if re.sub(r"\s+", " ", quote).strip() in source)
    if not evidence:
        return pending_assessment("상세 본문에서 판정 근거 문구를 확인하지 못함", fit_reason=fit.reason, checks=checks)
    corrected = replace(assessment, fit_reason=assessment.fit_reason or fit.reason,
                        missing_requirements=checks, evidence_quotes=evidence)
    if corrected.grade == "A" and (checks or corrected.verdict is not EligibilityVerdict.CONFIRMED or corrected.eligibility != "충족"):
        corrected = replace(corrected, grade="B", eligibility="미확인", verdict=EligibilityVerdict.NEEDS_CONFIRMATION)
    if corrected.grade == "B" and not corrected.missing_requirements:
        return pending_assessment("조건 확인 항목이 구체적으로 제시되지 않음", fit_reason=corrected.fit_reason)
    return corrected


def assessment_notification_status(program: dict, assessment: Assessment) -> str:
    """Final gate usable by LLM callers and preview/notification integration."""
    status = company_fit_status(program)
    if status != "eligible":
        return status
    if assessment.review_pending or assessment.grade == "REVIEW":
        return "pending"
    if assessment.grade not in ("A", "B") or assessment.eligibility == "미충족" or assessment.verdict in (
        EligibilityVerdict.INELIGIBLE, EligibilityVerdict.PIVOT_CANDIDATE,
    ):
        return "reject"
    if not assessment.fit_reason or not assessment.evidence_quotes:
        return "pending"
    if assessment.grade == "A" and (assessment.eligibility != "충족" or assessment.verdict is not EligibilityVerdict.CONFIRMED or assessment.missing_requirements):
        return "pending"
    if assessment.grade == "B" and not assessment.missing_requirements:
        return "pending"
    return "notify"


def stage2_assess(program: dict, detail_text: str) -> Assessment:
    detail_text = str(detail_text or "")
    fit = assess_company_fit(program, detail_text)
    if not fit.relevant:
        return Assessment("C", fit.reject_reason or fit.reason, "미확인",
                          EligibilityVerdict.INELIGIBLE if fit.reject_reason else EligibilityVerdict.NEEDS_CONFIRMATION)
    if fit.review_requirements:
        return pending_assessment("핵심 신청자격에 대한 회사 근거 확인 필요", fit_reason=fit.reason,
                                  checks=fit.missing_requirements)
    if not detail_text.strip():
        return pending_assessment("상세페이지 접근 불가", fit_reason=fit.reason)
    try:
        client = _get_gemini_client()
    except (ValueError, RuntimeError):
        client = None
    if client is None:
        return pending_assessment("자동 평가 연결을 확인하지 못함", fit_reason=fit.reason)
    try:
        response = client.models.generate_content(
            model="gemini-3.1-flash-lite-preview", contents=build_stage2_prompt(program, detail_text),
            config={"response_mime_type": "application/json", "response_schema": {
                "type": "object", "properties": {
                    "grade": {"type": "string", "enum": ["A", "B", "C", "REVIEW"]},
                    "reason": {"type": "string"},
                    "eligibility": {"type": "string", "enum": ["충족", "미확인", "미충족"]},
                    "verdict": {"type": "string", "enum": [v.value for v in EligibilityVerdict]},
                    "fit_reason": {"type": "string"},
                    "missing_requirements": {"type": "array", "items": {"type": "string"}},
                    "evidence_quotes": {"type": "array", "items": {"type": "string"}},
                }, "required": ["grade", "reason", "eligibility", "verdict", "fit_reason", "missing_requirements", "evidence_quotes"]}},
        )
        return apply_assessment_policy(program, parse_stage2_response(response.text), detail_text)
    except (AttributeError, TypeError, ValueError, RuntimeError, genai_errors.APIError) as error:
        logger.warning("Stage 2 assessment unavailable: %s", type(error).__name__)
        return pending_assessment("자동 평가 응답을 확인하지 못함", fit_reason=fit.reason)
