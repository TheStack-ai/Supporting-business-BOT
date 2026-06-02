import pytest
import json
from src.filters import calculate_score, is_recommended, is_obviously_irrelevant
from datetime import datetime, timedelta

@pytest.fixture
def profile():
    return {
        "interests": json.dumps(["AI", "Data"]),
        "include_keywords": json.dumps(["Startup", "Global"]),
        "exclude_keywords": json.dumps(["Spam"]),
        "region_allow": json.dumps(["Seoul"]),
        "min_score": 60,
        "due_days_threshold": 7
    }

def test_score_interest_match(profile):
    program = {
        "title": "AI Support Program",
        "summary_raw": "For Data Science",
        "category_l1": "IT"
    }
    score, reasons = calculate_score(program, profile)
    # Base 5 + Interest 25 = 30.
    # Wait, title has "AI" (match), summary has "Data" (match). Only counts once?
    # Logic: "if interest_hit: score += 25".
    # So 30.
    assert score >= 30
    assert "관심분야 일치" in reasons

def test_score_keyword_match(profile):
    program = {
        "title": "Global Startup Challenge",
        "summary_raw": "..."
    }
    score, reasons = calculate_score(program, profile)
    # Base 5
    # Include: Startup (+10), Global (+10). Total +20.
    # Total 25.
    assert score == 25
    assert "키워드 매칭(2건)" in reasons

def test_score_due_soon(profile):
    # Mock date
    today = datetime.now()
    due_date = (today + timedelta(days=3)).strftime("%Y-%m-%d")
    
    program = {
        "title": "Normal Program",
        "apply_end_at": due_date,
        "kind": "support"
    }
    score, reasons = calculate_score(program, profile)
    # Base 5 + Due 15 = 20.
    assert score == 20
    assert any("마감 임박" in r for r in reasons)

def test_exclude(profile):
    program = {
        "title": "Spam Program",
        "summary_raw": "..."
    }
    recommended, score, reasons = is_recommended(program, profile)
    assert not recommended
    assert score == 0


def test_hard_filter_rejects_obvious_consumer_sector():
    program = {
        "title": "K-수출전략품목 참여기업 모집(뷰티)",
        "summary_raw": "화장품 등 유망 소비재 글로벌 진출",
    }

    rejected, reason = is_obviously_irrelevant(program)

    assert rejected
    assert "뷰티" in reason


def test_hard_filter_rejects_consumer_channel_without_relevance():
    program = {
        "title": "TV홈쇼핑 입점지원 사업",
        "summary_raw": "온라인쇼핑몰 판매 지원",
    }

    rejected, reason = is_obviously_irrelevant(program)

    assert rejected
    assert "홈쇼핑" in reason


def test_hard_filter_keeps_mro_public_procurement_notice():
    program = {
        "title": "MRO 납품 중소기업 공공조달 입찰 컨설팅",
        "summary_raw": "공공조달 시장 진입 지원",
    }

    rejected, reason = is_obviously_irrelevant(program)

    assert not rejected
    assert reason == ""


def test_hard_filter_rejects_consumer_storefront_notice():
    program = {
        "title": "스마트 플래그십 스토어(소담상회 with 무신사)",
        "summary_raw": "스토어 내 제품 체험·전시 공간 제공",
    }

    rejected, reason = is_obviously_irrelevant(program)

    assert rejected
    assert "무신사" in reason


def test_hard_filter_keeps_trial_purchase_notice():
    program = {
        "title": "동반성장몰 시범구매 제품 연계",
        "summary_raw": "시범구매 및 상생협력제품 판로지원",
    }

    rejected, reason = is_obviously_irrelevant(program)

    assert not rejected
    assert reason == ""
