from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from src.company_fit import assess_company_fit, company_fit_status, applicant_region_conflict
from src.filters import is_obviously_irrelevant, is_recommended
from src.llm_filter import (
    Assessment, EligibilityVerdict, apply_assessment_policy,
    assessment_notification_status, parse_stage2_response, stage1_quick_filter, stage2_assess,
)
from src.program_selection import prioritize_programs, run_keyword_fallback


def notice(title, summary="", key="support:1", **kwargs):
    return {"program_key": key, "title": title, "summary_raw": summary, "kind": "support", **kwargs}


@pytest.mark.parametrize("title,summary", [
    ("차량방호 볼라드 실증 지원", "공공시설 현장 실증"),
    ("제조 중소기업 KC·UL 해외인증 지원", "경기도 소재 제조기업 대상"),
    ("중소기업 R&D 지원", "기술개발 및 시제품 제작"),
    ("스마트공장 공정개선 지원", "제조기업 대상"),
    ("산업재 수출바우처", "중소 제조기업의 해외 판로 지원"),
    ("공공조달 입찰 컨설팅", "MRO 시장 진출"),
    ("제조 현장 AI 검사 자동화 지원", "중소기업 생산성 개선"),
    ("의료시설용 차량방호·출입통제 실증", "병원에 납품하는 제조기업 지원"),
])
def test_company_fit_keeps_concrete_product_and_business_benefits(title,summary):
    p=notice(title,summary)
    assert company_fit_status(p)=="eligible"
    assert assess_company_fit(p).score>=60
    assert is_obviously_irrelevant(p)==(False,"")


@pytest.mark.parametrize("title,summary", [
    ("손짓으로 여는 AI 미디어아트 제작", "AI 교육"),
    ("판교 퇴근길 밋업", "경기 소재 벤처기업 네트워킹"),
    ("TV홈쇼핑 수출 인증 지원", "화장품 소비재 해외 진출"),
    ("부산 근로기준법 노무특강", "일반 사업주 대상 교육"),
    ("K뷰티 해외 수출", "화장품기업 대상"),
])
def test_generic_or_consumer_events_do_not_become_recommendations(title,summary):
    p=notice(title,summary)
    assert company_fit_status(p)=="reject"
    assert not is_recommended(p,{"min_score":1,"region_allow":"[\"전국\"]"})[0]


def test_applicant_region_conflict_is_not_bypassed_by_nationwide_preference():
    p=notice("경남 무역아카데미", "경남 소재 수출기업 대상")
    assert "경남" in applicant_region_conflict(p)
    assert not is_recommended(p,{"region_allow":"[\"전국\"]","min_score":0})[0]
    assert run_keyword_fallback([p],{"region_allow":"[\"전국\"]","min_score":0})==[]


def test_venue_and_organizer_region_do_not_exclude_nationwide_event():
    p=notice("[경북] 온라인 CBAM 설명회", "전국 중소기업의 탄소국경 규제 대응. 장소: 대구.",agency="경북경제진흥원")
    assert not applicant_region_conflict(p)
    assert company_fit_status(p)=="eligible"


def test_region_alternatives_and_city_specific_restrictions():
    assert not applicant_region_conflict(notice("제조 지원", "서울 및 경기 소재 기업 대상"))
    assert applicant_region_conflict(notice("제조 지원", "경기도 김포시 소재 제조기업 대상"))
    assert not applicant_region_conflict(notice("제조 지원", "경기도 화성시 소재 제조기업 대상"))


def test_medical_facility_market_does_not_mean_medical_applicant():
    assert company_fit_status(notice("병원용 출입통제 실증", "공공 의료시설에 납품할 제품 지원"))=="eligible"
    assert company_fit_status(notice("의료기기 제조기업 대상 출입통제 실증", "의료기기 기업만 신청"))=="reject"


def test_name_does_not_establish_female_enterprise_or_startup_eligibility():
    p=notice("여성기업 차량방호 시제품 지원", "여성대표 기업 대상")
    assert company_fit_status(p)=="pending"
    assert stage2_assess(p,"여성기업 대상 지원").review_pending
    q=notice("청년창업 제조 R&D", "청년 대표자만 신청")
    assert company_fit_status(q)=="pending"
    invented=Assessment("A","여성 대표(박채영)로 자격 충족","충족",EligibilityVerdict.CONFIRMED,
                        fit_reason="차량방호 실증",evidence_quotes=("제조기업 대상",))
    assert apply_assessment_policy(notice("볼라드 실증"),invented,"제조기업 대상").review_pending


@pytest.mark.parametrize("summary", [
    "지원대상: 중소 제조기업. 여성기업 가점/우대(필수 아님)",
    "지원대상: 전체 제조기업. 우대대상: 여성기업",
    "지원대상: 일반 제조기업, 여성기업 등",
    "지원대상: 여성기업 또는 일반제조기업",
    "지원대상: 일반 제조기업 및 여성기업",
    "지원대상: 여성기업 또는 일반제조기업. 시험인증 완료는 필수",
    "여성기업 우대, 가점 신청 시 확인서 제출 필수",
])
def test_optional_or_alternative_qualification_does_not_block_general_manufacturer(summary):
    p=notice("중소 제조기업 KC 인증 지원",summary)
    assert company_fit_status(p)=="eligible"
    assert not assess_company_fit(p).review_requirements


def test_optional_title_and_navigation_mentions_are_not_eligibility():
    p=notice("여성기업 우대 KC 인증 지원", "전체 제조기업 신청 가능")
    assert company_fit_status(p)=="eligible"
    generic=notice("KC 인증 지원", "제조기업의 해외규격 취득을 지원합니다")
    navigation="지원사업 분류 | 여성기업 | 청년창업 | 장애인기업\n지원대상: 전국 제조기업"
    assert company_fit_status(generic,navigation)=="eligible"
    assert company_fit_status(generic,"지원대상: 전체 제조기업. 메뉴: 여성기업 청년창업 장애인기업")=="eligible"


@pytest.mark.parametrize("title,summary", [
    ("여성기업 KC 인증 지원", "제조기업 대상"),
    ("제조기업 KC 인증 지원", "지원대상: 여성기업만 신청 가능"),
    ("제조기업 KC 인증 지원", "지원대상: 일반 제조기업 중 여성기업만 신청 가능"),
    ("제조 R&D 지원", "신청자격: 창업 후 7년 이내 기업"),
    ("제조 R&D 지원", "신청자격:\n창업 후 7년 이내 기업"),
    ("제조 R&D 지원", "창업 후 7년 이내 기업을 대상으로 지원"),
    ("산업재 수출 지원", "신청대상: 수출실적 보유 기업"),
    ("산업재 수출 지원", "수출실적 필수"),
    ("제조기업 KC 인증 지원", "신청자격: 여성기업 해당 여부 확인 필요"),
])
def test_dedicated_or_genuine_unknown_applicant_qualification_remains_pending(title,summary):
    p=notice(title,summary)
    assert company_fit_status(p)=="pending"
    assert assess_company_fit(p).review_requirements


def test_womens_ceo_title_still_records_unverified_qualification():
    p=notice("여성벤처CEO 혁신아카데미", "기업 대표 교육")
    assert "여성대표·여성기업 자격 확인" in assess_company_fit(p).review_requirements


def test_relevance_precedes_deadline_and_capacity_is_bounded():
    urgent=(datetime.now()+timedelta(days=1)).strftime("%Y-%m-%d")
    noise=[notice("AI 미디어아트 교육",key=f"e:{i}",apply_end_at=urgent) for i in range(45)]
    useful=notice("KC 인증 비용 지원",key="good",apply_end_at="2099-01-01")
    assert [p["program_key"] for p in prioritize_programs(noise+[useful],40)]==["good"]
    changes=[notice("볼라드 실증",key=f"c:{i}",_change_kind="CHANGED") for i in range(50)]
    assert len(prioritize_programs(changes,40))==40


def test_relevant_training_does_not_outrank_direct_certification_support():
    generic_training=notice("공공조달 글로벌 무역금융 AI 교육", "제조기업의 수출 준비", key="training", kind="event")
    certificate=notice("제조 중소기업 KC·UL 인증비 지원", "제품 해외규격 취득 비용 지원", key="certificate")
    practical=notice("나라장터 입찰 실무·제도 설명회", "공공조달 등록과 규정 안내", key="practical", kind="event")
    assert company_fit_status(generic_training)=="eligible"
    assert company_fit_status(practical)=="eligible"
    assert assess_company_fit(certificate).score > assess_company_fit(practical).score > assess_company_fit(generic_training).score
    assert [p["program_key"] for p in prioritize_programs([generic_training,practical,certificate],2)]==["certificate","practical"]


def test_failed_stage1_only_keeps_relevant_candidates_pending(monkeypatch):
    from src import llm_filter
    client=MagicMock();client.models.generate_content.side_effect=RuntimeError("offline unavailable")
    monkeypatch.setattr(llm_filter,"_get_gemini_client",lambda:client)
    good=notice("볼라드 실증",key="good")
    irrelevant=notice("퇴근길 밋업",key="bad")
    result=stage1_quick_filter([good,irrelevant])
    assert result==[] and result.pending_keys=={"good"} and result.rejected_keys=={"bad"}


@pytest.mark.parametrize("payload", [
    {"grade":"A","reason":"대상 아님","eligibility":"미충족","verdict":"확인됨"},
    {"grade":"B","reason":"대상 아님","eligibility":"미확인","verdict":"신청 불가"},
    {"grade":"A","reason":"업종 변경 필요","eligibility":"충족","verdict":"사업전환 후보"},
])
def test_inconsistent_eligibility_is_never_sent(payload):
    a=parse_stage2_response(payload)
    assert a.grade=="C"
    assert assessment_notification_status(notice("볼라드 실증"),a)=="reject"


def test_conditional_notice_requires_specific_checks_and_source_evidence():
    p=notice("KC 인증 지원", "제조기업 대상")
    a=Assessment("B","시험비 지원","미확인",EligibilityVerdict.NEEDS_CONFIRMATION,
                 fit_reason="차량차단기의 시험비 절감",missing_requirements=("인증서 유효기간",),
                 evidence_quotes=("제조기업 대상",))
    reviewed=apply_assessment_policy(p,a,"제조기업 대상 시험비 지원")
    assert assessment_notification_status(p,reviewed)=="notify"
    no_evidence=Assessment("A","충족","충족",EligibilityVerdict.CONFIRMED,
                           fit_reason="시험비 절감",evidence_quotes=("본문에 없는 인용",))
    assert apply_assessment_policy(p,no_evidence,"제조기업 대상").review_pending
    vague=Assessment("B","조금 관련됨","미확인",evidence_quotes=("제조기업 대상",))
    assert apply_assessment_policy(p,vague,"제조기업 대상").review_pending


def test_historical_financial_data_does_not_establish_current_eligibility():
    p=notice("제조기업 KC 인증 지원", "전년도 매출 30억원 이하 기업 대상")
    a=Assessment("A","2022년 매출 18.34억원이므로 충족","충족",EligibilityVerdict.CONFIRMED,
                 evidence_quotes=("전년도 매출 30억원 이하",))
    reviewed=apply_assessment_policy(p,a,"전년도 매출 30억원 이하 기업 대상")
    assert reviewed.grade=="B" and reviewed.verdict is EligibilityVerdict.NEEDS_CONFIRMATION
    assert any("현재 매출" in check for check in reviewed.missing_requirements)


def test_keyword_fallback_uses_the_same_positive_and_negative_gates():
    profile={"region_allow":"[\"전국\"]","interests":"[]","include_keywords":"[]","min_score":60}
    notices=[notice("볼라드 실증",key="good"),notice("경남 수출 바우처","경남 소재 기업 대상",key="region"),
             notice("홈쇼핑 해외 인증",key="consumer"),notice("여성기업 KC 인증",key="unknown")]
    assert [r["item"]["program_key"] for r in run_keyword_fallback(notices,profile)]==["good"]
