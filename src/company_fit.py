"""Company-specific relevance and explicit eligibility checks, without model guesses.

The company operates vehicle-protection/access-control equipment from Anyang and
Hwaseong. A venue or an organizer's address is not an applicant location rule.
Unknown personal/company qualifications are never inferred from a person's name.
"""
from __future__ import annotations

from dataclasses import dataclass
import html
import re


@dataclass(frozen=True, slots=True)
class CompanyFit:
    score: int
    reason: str
    reject_reason: str = ""
    missing_requirements: tuple[str, ...] = ()
    review_requirements: tuple[str, ...] = ()

    @property
    def relevant(self) -> bool:
        return self.score >= 60 and not self.reject_reason


def _text(program: dict, detail_text: str = "") -> str:
    # Agency/region metadata frequently names the host, not the eligible firm.
    raw = " ".join(str(program.get(key) or "") for key in ("title", "summary_raw"))
    raw += " " + str(detail_text or "")
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", raw))).strip()


_REGION_NAMES = (
    "서울특별시", "부산광역시", "대구광역시", "인천광역시", "광주광역시", "대전광역시", "울산광역시",
    "세종특별자치시", "강원특별자치도", "전북특별자치도", "제주특별자치도", "경상남도", "경상북도",
    "전라남도", "전라북도", "충청남도", "충청북도", "경기도", "강원도", "수도권", "전국",
    "안양시", "화성시", "김포시", "수원시", "성남시", "용인시", "고양시", "부천시", "평택시", "안산시",
    "서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "경남", "경북", "전남", "전북",
    "충남", "충북", "경기", "강원", "제주", "안양", "화성", "김포", "수원", "성남", "용인", "고양", "부천",
)
_REGION = "(?:" + "|".join(map(re.escape, _REGION_NAMES)) + ")"
_REGION_LIST = _REGION + r"(?:(?:\s*(?:및|또는|혹은|·|,|/|ㆍ|와|과)\s*|\s+)" + _REGION + r")*"
_APPLICANT = r"(?:[가-힣A-Za-z0-9·()\-]{0,10}\s*){0,2}(?:기업|사업자|사업체|소상공인|제조사)"
_LOCATION_RULES = (
    re.compile(r"(?P<regions>" + _REGION_LIST + r")\s*(?:지역\s*내|지역|관내|도내|시내)?\s*(?:에\s*)?(?:소재(?:한)?|본사를\s*둔|사업장을\s*둔)\s*" + _APPLICANT),
    re.compile(r"(?:지원대상|신청대상|신청자격|참여대상)\s*[:：]?\s*(?P<regions>" + _REGION_LIST + r")\s*(?:관내|도내|지역)?\s*" + _APPLICANT),
)
_ALLOWED = {"경기도", "경기", "안양시", "안양", "화성시", "화성", "수도권", "전국"}


def applicant_region_conflict(program: dict, detail_text: str = "") -> str:
    text = _text(program, detail_text)
    for pattern in _LOCATION_RULES:
        for match in pattern.finditer(text):
            regions = re.findall(_REGION, match.group("regions"))
            # In "경기도 김포시", the city is the actual restriction. In
            # "서울 또는 경기", either alternative is sufficient.
            restrictive_cities = [r for r in regions if r.endswith(("시", "군", "구"))
                                  and not r.endswith(("광역시", "특별시", "자치시"))]
            permitted = any(r in _ALLOWED for r in (restrictive_cities or regions))
            if regions and not permitted:
                return "신청 기업 소재지 요건(" + match.group("regions") + ")이 본사 안양·공장 화성과 맞지 않음"
    return ""


_PRODUCT = re.compile(r"볼라드|차량\s*(?:방호|차단|돌진)|대테러|출입\s*통제|방호\s*(?:장비|시설)|보안장비|교통\s*(?:통제|신호|안전시설)|스쿨존|보행자\s*안전|산업용\s*제어|경보장치|디자인\s*울타리", re.I)
_INDUSTRIAL = re.compile(r"산업재|산업용|제조(?:업|기업|중소기업|공정|현장)?|금속|전자부품|하드웨어|공장|기계설비|B2B|MRO", re.I)
_PROCUREMENT = re.compile(r"공공조달|혁신조달|조달청|나라장터|우수조달|시범구매|혁신제품|다수공급자|성능인증|직접생산|MRO", re.I)
_CERTIFICATION = re.compile(r"해외\s*규격|해외\s*인증|시험\s*[·ㆍ/]?\s*인증|제품\s*인증|국제\s*인증|시험성적|충돌시험|\bKC\b|\bCE\b|\bUL(?:508A)?\b|EMC|IWA\s*14|PAS\s*68|ISO\s*(?:9001|14001|45001)", re.I)
_TECHNOLOGY = re.compile(r"R\s*&\s*D|연구개발|기술개발|시제품|기술사업화|실증|스마트공장|스마트제조|공정개선", re.I)
_EXPORT = re.compile(r"수출|해외진출|해외\s*진출|바이어|CBAM|탄소국경|FTA", re.I)
_CONSUMER = re.compile(r"뷰티|화장품|푸드|식품|패션|의류|농식품|농수산|수산물|미용|홈쇼핑|온라인\s*쇼핑몰|스마트스토어|디지털커머스|판판셀러|소담스퀘어|무신사|면세점|면세장|관광", re.I)
_IRRELEVANT_EVENT = re.compile(r"미디어\s*아트|문화예술|취미|퇴근길\s*밋업|친목|일반\s*네트워킹", re.I)
_CORE_UNKNOWN = (
    (re.compile(r"여성\s*(?:벤처)?\s*(?:CEO|대표|기업인|창업)|여성기업|여성벤처", re.I), "여성대표·여성기업 자격 확인"),
    (re.compile(r"청년\s*(?:창업|기업|CEO|대표)|예비\s*창업|초기\s*창업|창업\s*도약|업력\s*\d+\s*년\s*(?:이내|미만)|창업\s*\d+\s*년\s*(?:이내|미만)", re.I), "대표 연령·창업연차 요건 확인"),
    (re.compile(r"사회적기업\s*(?:전용|대상|만)|장애인기업|재창업\s*(?:자|기업)|재도전\s*성공패키지", re.I), "특수 기업유형·재창업 자격 확인"),
    (re.compile(r"수출실적\s*(?:보유|필수|있는|\d)|수출\s*실적이\s*있는", re.I), "기존 수출실적 요건 확인"),
)


def assess_company_fit(program: dict, detail_text: str = "") -> CompanyFit:
    text = _text(program, detail_text)
    region_conflict = applicant_region_conflict(program, detail_text)
    if region_conflict:
        return CompanyFit(0, "", region_conflict)

    product, industrial = bool(_PRODUCT.search(text)), bool(_INDUSTRIAL.search(text))
    procurement, certification = bool(_PROCUREMENT.search(text)), bool(_CERTIFICATION.search(text))
    # A hospital is a market for access-control products, not proof that the
    # applicant must be a medical company. Explicit sector-only eligibility wins.
    sector_only = re.search(r"(?:의료기기|바이오|식품|화장품|관광|섬유)\s*(?:분야\s*|업종\s*)?(?:제조기업|기업|제조업체|사업자)\s*(?:만|한정|전용|대상|을\s*위한|에\s*한함)", text)
    if sector_only:
        return CompanyFit(0, "", "다른 업종 신청기업으로 한정: " + sector_only.group())
    consumer = _CONSUMER.search(text)
    if consumer and not product and not procurement:
        return CompanyFit(0, "", "소비재·소매 판로 중심(" + consumer.group() + ")")
    if _IRRELEVANT_EVENT.search(text) and not product and not procurement and not certification:
        return CompanyFit(0, "", "제품·제조 사업과 연결되지 않는 문화·일반 교류 행사")

    score, reason = 0, "회사 제품·제조·조달 사업에 대한 구체적인 활용 근거 없음"
    if product:
        score, reason = 95, "차량방호·출입통제·교통안전 제품의 개발·실증·납품에 직접 활용"
    elif procurement:
        score, reason = 90, "공공시설 대상 조달·산업재 판로 확대에 활용"
    elif certification:
        score, reason = 85, "방호·제어 장비의 시험·제품인증 및 해외 규격 대응에 활용"
    elif _TECHNOLOGY.search(text):
        score, reason = 82 if industrial else 75, "제조 제품의 연구개발·시제품·실증 또는 생산공정 개선에 활용"
    elif _EXPORT.search(text) and (industrial or re.search(r"중소기업|수출바우처|규제|통상|무역|CBAM|탄소국경|FTA", text, re.I)):
        score, reason = 76, "산업 장비의 해외 판로·수출 준비 및 통상 대응에 활용"
    elif re.search(r"AI|인공지능|디지털전환|\bDX\b", text, re.I) and re.search(r"제조|공정|검사|설계|현장|업무자동화|생산성", text):
        score, reason = 78, "제품 설계·제조 현장의 자동화와 생산성 개선에 활용"
    elif re.search(r"특허|지식재산|IP\s*(?:나래|디딤돌)", text, re.I) and re.search(r"지원|컨설팅|출원|분석|기술|보호", text):
        score, reason = 70, "방호·제어 기술의 특허 확보와 기술 보호에 활용"
    elif re.search(r"시설자금|정책자금|설비투자|운전자금", text) and re.search(r"중소기업|제조|공장|설비", text):
        score, reason = 65, "제조기업의 설비·운영 자금 조달에 활용"
    elif industrial and re.search(r"지원|컨설팅|개선|사업화|생산|규제|안전보건", text):
        score, reason = 65, "산업 장비 제조와 현장 운영 개선에 활용"

    review = tuple(label for pattern, label in _CORE_UNKNOWN if pattern.search(text))
    checks = list(review)
    if re.search(r"매출(?:액)?\s*(?:이|은|가)?\s*(?:최근|전년|연간|평균|\d)|\d+\s*억\s*(?:원)?\s*(?:이하|미만|이상)", text):
        checks.append("현재 매출과 해당 공고 기준연도 확인(2022년 매출로 대체 불가)")
    if re.search(r"(?:종업원|상시\s*(?:근로자|근로인원)|직원)\s*(?:수)?\s*\d|\d+\s*인\s*(?:이하|미만|이상)", text):
        checks.append("현재 상시근로자 수 확인(기준일 없는 6명으로 확정 불가)")
    return CompanyFit(score, reason, missing_requirements=tuple(dict.fromkeys(checks)), review_requirements=review)


def company_fit_status(program: dict, detail_text: str = "") -> str:
    fit = assess_company_fit(program, detail_text)
    if not fit.relevant:
        return "reject"
    return "pending" if fit.review_requirements else "eligible"
