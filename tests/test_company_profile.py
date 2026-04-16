from src.company_profile import COMPANY_PROFILE, build_stage1_prompt, build_stage2_prompt

def test_company_profile_contains_key_info():
    assert "싸이베리어" in COMPANY_PROFILE
    assert "28901" in COMPANY_PROFILE
    assert "안양" in COMPANY_PROFILE
    assert "6명" in COMPANY_PROFILE

def test_build_stage1_prompt():
    programs = [
        {"title": "중소기업 수출 지원", "summary_raw": "수출 바우처"},
        {"title": "수산식품 인증", "summary_raw": "수산물 가공"},
    ]
    prompt = build_stage1_prompt(programs)
    assert "싸이베리어" in prompt
    assert "1. 중소기업 수출 지원 | 수출 바우처" in prompt
    assert "2. 수산식품 인증 | 수산물 가공" in prompt
    assert "PASS" in prompt and "REJECT" in prompt

def test_build_stage2_prompt():
    program = {"title": "정보보호 인증제품 추천"}
    detail_text = "정보보호 인증을 보유한 중소기업 대상..."
    prompt = build_stage2_prompt(program, detail_text)
    assert "싸이베리어" in prompt
    assert "정보보호 인증제품 추천" in prompt
    assert "정보보호 인증을 보유한 중소기업 대상" in prompt

def test_stage1_prompt_handles_none_summary():
    programs = [{"title": "테스트", "summary_raw": None}]
    prompt = build_stage1_prompt(programs)
    assert "1. 테스트 |" in prompt
