# src/company_profile.py

COMPANY_PROFILE = """회사명: (주)싸이베리어 / CyBarrier Co Ltd
대표: 박채영
업종코드: 전기경보·신호장치 제조(28901), 금속구조물 제조(25112), 기타 전자부품 제조(26422), 기타 산업용 전기장비 제조(28123), 특수목적용 자동차 제조(30203)
업태: 전기경보·신호장치 제조, 금속구조물 제조, 건물설비설치공사, 패키지SW 개발
제품: 차량방호 볼라드, 대테러 차량차단기(KC), 출입통제시스템(KC), ACPCS 차량방호 제어시스템, 교통통제 안전펜스, 디자인 울타리
인증: KC(차량차단기, 출입통제소 서버), UL508A, ISO 9001/14001/45001, IMSA
기업유형: 소기업(소상공인), 벤처기업(혁신성장유형)
소재지: 본사 경기 안양시 동안구, 공장 경기 화성시 양감면
상시종업원: 6명
매출: 18.34억원 (2022)
면허: 금속·창호·지붕·건축물조립공사업
등록: 직접생산확인(볼라드, 출입통제시스템, 차량차단기, 디자인형울타리), 소프트웨어사업자, 자동차제작자등록(E65, 특수용도 완성차)"""

_STAGE1_SYSTEM = """당신은 정부지원사업 사전 분류기입니다.
아래 회사 프로필을 참고하여, 각 공고가 이 회사에 조금이라도 관련될 수 있는지 판단하세요.

[회사 프로필]
{profile}

[판단 기준]
- 회사의 업종(제조업, 보안장비, 건설, SW), 규모(소기업), 소재지(경기)와 조금이라도 접점이 있으면 PASS
- 식품, 농수산, 뷰티, 섬유, 의료/바이오, 관광 등 명백히 다른 업종만 REJECT
- 일반적 중소기업 지원(수출, 인증, 디지털전환, R&D 등)은 PASS
- 판단이 애매하면 반드시 PASS"""

_STAGE2_SYSTEM = """당신은 정부지원사업 적격성 심사관입니다.
아래 공고의 상세 내용을 읽고, 이 회사가 실제로 지원 가능한지 판단하세요.

[회사 프로필]
{profile}

[판단 항목]
1. 업종 요건: 공고가 요구하는 업종과 회사 업종의 일치 여부
2. 규모 요건: 매출, 종업원수, 기업유형 요건 충족 여부
3. 지역 요건: 소재지 제한이 있다면 충족 여부
4. 기타 자격: 특수 인증, 경력, 사전 등록 등

등급 기준:
A = 자격 요건이 충족되고 회사 사업과 직접 관련됨
B = 일부 관련되거나 자격 요건을 공고만으로 확인할 수 없음
C = 자격 미충족이 명확하거나 사업 영역과 무관함

reason은 핵심 정보만 포함 (지원금액, 대상, 마감일 등)."""


def build_stage1_prompt(programs: list[dict]) -> str:
    lines = []
    for i, p in enumerate(programs, 1):
        title = (p.get("title") or "").strip()
        summary = (p.get("summary_raw") or "").strip()
        lines.append(f"{i}. {title} | {summary}")

    system = _STAGE1_SYSTEM.format(profile=COMPANY_PROFILE)
    listing = "\n".join(lines)
    return f"{system}\n\n[공고 목록]\n{listing}"


def build_stage2_prompt(program: dict, detail_text: str) -> str:
    title = (program.get("title") or "").strip()
    system = _STAGE2_SYSTEM.format(profile=COMPANY_PROFILE)
    return f"{system}\n\n[공고 상세]\n제목: {title}\n내용:\n{detail_text}"
