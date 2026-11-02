import json
from datetime import datetime
from typing import Dict, List, Any, Tuple

from src.company_fit import assess_company_fit, applicant_region_conflict

HARD_EXCLUDE_KEYWORDS = [
    "뷰티",
    "푸드",
    "패션",
    "식품",
    "농수산",
    "수산물",
    "농식품",
    "화장품",
    "미용",
    "의류",
    "섬유",
    "관광",
    "의료",
    "바이오",
]

CONSUMER_CHANNEL_KEYWORDS = [
    "tv홈쇼핑",
    "홈쇼핑",
    "온라인쇼핑몰",
    "온라인 쇼핑몰",
    "스마트스토어",
    "md 상담",
    "디지털커머스",
    "판판셀러",
    "소담스퀘어",
    "무신사",
    "플래그십 스토어",
    "동반성장몰",
    "전용판매장",
    "면세점",
    "면세장",
]

DIRECT_RELEVANCE_KEYWORDS = [
    "mro",
    "공공조달",
    "조달",
    "입찰",
    "제조",
    "스마트제조",
    "보안",
    "차량",
    "방호",
    "차단",
    "볼라드",
    "출입통제",
    "안전",
    "수출",
    "해외",
    "인증",
    "kc",
    "r&d",
    "기술개발",
    "시범구매",
    "소프트웨어",
    "sw",
]


def _combined_text(program: Dict[str, Any]) -> str:
    text_fields = [
        program.get('title', ''),
        program.get('summary_raw', ''),
        program.get('agency', ''),
        program.get('category_l1', ''),
        program.get('region_raw', ''),
    ]
    return " ".join([str(t) for t in text_fields if t]).lower()


def is_obviously_irrelevant(program: Dict[str, Any]) -> Tuple[bool, str]:
    """Reject explicit incompatibilities; broad low fit is ranked separately."""
    fit = assess_company_fit(program)
    return bool(fit.reject_reason), fit.reject_reason


def check_exclude(program: Dict[str, Any], profile: Dict[str, Any]) -> bool:
    raw = profile.get('exclude_keywords', '[]')
    if not raw or not raw.strip():
        raw = '[]'
    try:
        excludes = json.loads(raw)
    except json.JSONDecodeError:
        excludes = []
        
    if not excludes:
        return False
        
    text_fields = [
        program.get('title', ''),
        program.get('summary_raw', ''),
        program.get('agency', ''),
        program.get('url', '')
    ]
    combined = " ".join([t for t in text_fields if t]).lower()
    
    for kw in excludes:
        if kw.lower() in combined:
            return True
    return False

def check_region(program: Dict[str, Any], profile: Dict[str, Any]) -> bool:
    # The stored preference "전국" means nationwide opportunities are welcome;
    # it cannot satisfy a notice restricted to firms established elsewhere.
    return bool(applicant_region_conflict(program))


def calculate_score(program: Dict[str, Any], profile: Dict[str, Any]) -> Tuple[int, List[str]]:
    score = 5 # Base score
    reasons = []
    
    raw_interests = profile.get('interests', '[]')
    if not raw_interests or not raw_interests.strip(): raw_interests = '[]'
    try: interests = json.loads(raw_interests)
    except: interests = []
    
    raw_includes = profile.get('include_keywords', '[]')
    if not raw_includes or not raw_includes.strip(): raw_includes = '[]'
    try: includes = json.loads(raw_includes)
    except: includes = []
    
    min_score = profile.get('min_score', 60)
    due_threshold = profile.get('due_days_threshold', 7)
    
    # Text for matching
    title = program.get('title', '') or ''
    summary = program.get('summary_raw', '') or ''
    category = program.get('category_l1', '') or ''
    text_content = (title + " " + summary + " " + category).lower()
    
    # 1. Interests matching (+25)
    # Check if any interest keyword is in text
    interest_hit = False
    for interest in interests:
        if interest.lower() in text_content:
            interest_hit = True
            break
    if interest_hit:
        score += 25
        reasons.append("관심분야 일치")
        
    # 2. Include keywords (+10 each, max +30)
    include_hits = 0
    for kw in includes:
        if kw.lower() in text_content:
            include_hits += 1
    
    score_add = min(include_hits * 10, 30)
    if score_add > 0:
        score += score_add
        reasons.append(f"키워드 매칭({include_hits}건)")
        
    # 3. Due soon (+15)
    days_left = get_days_left(program)
    if days_left is not None and days_left <= due_threshold and days_left >= 0:
        score += 15
        reasons.append(f"마감 임박: D-{days_left}")
    
    # 4. Region match (Bonus rationale, no score change in PRD? "only when confidently true")
    # PRD says "Region match" is a reason.
    # Let's add it to reasons but not score? PRD says just "Reasons: ...".
    # Let's check region match for reason.
    try:
        allows = json.loads(profile.get('region_allow') or '[]')
    except (ValueError, TypeError):
        allows = []
    region_raw = program.get('region_raw', '')
    if region_raw and allows:
        for region in allows:
            if region in region_raw:
                reasons.append(f"지역 조건 충족({region})")
                break

    fit = assess_company_fit(program)
    if fit.relevant:
        score = max(score, fit.score)
        reasons.insert(0, fit.reason)
    # Clamp
    final_score = max(0, min(100, score))
    return final_score, reasons

def get_days_left(program: Dict[str, Any]) -> int:
    # return int or None
    # For support: apply_end_at
    # For event: apply_end_at (preferred) or event_end_at (if apply not avail? PRD says apply_end priority)
    
    end_at = program.get('apply_end_at')
    if not end_at and program.get('kind') == 'event':
        # Fallback to event end? PRD says "Event: apply_period... based on receipt period. Event period separate."
        # If receipt period parsing failed, due logic skipped.
        return None
        
    if not end_at:
        return None
        
    try:
        dt_end = datetime.strptime(end_at, "%Y-%m-%d")
        d_end = dt_end.date()
        d_now = datetime.now().date()
        return (d_end - d_now).days
    except:
        return None

def is_recommended(program: Dict[str, Any], profile: Dict[str, Any]) -> Tuple[bool, int, List[str]]:
    # Neither deadline bonuses nor broad profile keywords establish business fit.
    fit = assess_company_fit(program)
    if not fit.relevant or fit.review_requirements:
        return False, 0, list(fit.missing_requirements)
    # 1. Hard filters
    if check_exclude(program, profile):
        return False, 0, []
        
    if check_region(program, profile): # Returns True if excluded
        return False, 0, []
        
    # Check if end date passed
    days_left = get_days_left(program)
    if days_left is not None and days_left < 0:
        return False, 0, []
        
    # 2. Score
    score, reasons = calculate_score(program, profile)
    min_score = profile.get('min_score', 60)
    
    if score >= min_score:
        return True, score, reasons
    
    return False, score, reasons
