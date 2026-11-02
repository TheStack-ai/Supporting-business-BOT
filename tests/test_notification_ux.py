import html
from datetime import datetime
from html.parser import HTMLParser
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src.llm_filter import Assessment, EligibilityVerdict
from src.notification_format import (
    deadline_label, format_fallback_message, format_graded_message,
    messages_with_coverage, safe_url, utf16_length, with_coverage,
)
from src.source_coverage import format_coverage_manifest
from src.source_result import CollectionStatus, CoverageEntry


class RenderedHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.text = []
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        assert tag in ('b', 'i', 'a')
        self.stack.append(tag)
        if tag == 'a':
            self.hrefs.append(dict(attrs)['href'])

    def handle_endtag(self, tag):
        assert self.stack and self.stack.pop() == tag

    def handle_data(self, text):
        self.text.append(text)


def validate(message, limit=4000):
    assert utf16_length(message) <= limit
    parser = RenderedHTML(); parser.feed(message)
    assert parser.stack == []
    return parser


def program(**overrides):
    return {'program_key': 'support:one', 'title': '차량차단기 해외규격 인증 지원',
            'kind': 'support', 'source': 'bizinfo', 'url': 'https://example.com/a?x=1&y=2',
            'apply_end_at': '2026-11-09T18:00:00+09:00', '_change_kind': 'NEW', **overrides}


def test_cards_show_fit_deadline_and_conditions_without_technical_jargon():
    a = SimpleNamespace(grade='A', reason='시험비 최대 1억원', eligibility='충족',
                        verdict=EligibilityVerdict.CONFIRMED, fit_reason='차량차단기 UL 시험·인증 비용 지원',
                        missing_requirements=())
    b = SimpleNamespace(grade='B', reason='수출 지원', eligibility='미확인',
                        verdict=EligibilityVerdict.NEEDS_CONFIRMATION, fit_reason='산업용 방호장비 해외 판로 발굴',
                        missing_requirements=('최근 수출 실적 확인',))
    body = format_graded_message([(program(), a)], [(program(title='산업재 해외상담회', kind='event'), b)], 40, 12)
    text = ''.join(validate(body).text)
    assert '우선 검토 1건 · 요건 확인 1건' in text
    assert '활용  차량차단기 UL 시험·인증 비용 지원' in text
    assert '확인할 점  최근 수출 실적 확인' in text
    assert '마감 11/09 18:00' in text and 'KST' in text
    assert '반드시' not in text and 'LLM' not in text
    assert len(validate(body).hrefs) == 2


def test_kst_deadline_and_unknown_event_registration_are_distinct():
    now = datetime(2026, 11, 2, 23, 30, tzinfo=ZoneInfo('Asia/Seoul'))
    assert deadline_label(program(apply_end_at='2026-11-03T09:00:00Z'), now) == '마감 11/03 18:00 · D-1'
    assert deadline_label(program(apply_end_at='2026-11-02'), now) == '마감 11/02 · 오늘 마감'
    assert deadline_label(program(apply_end_at=None), now) == '접수 마감 미확인'
    assert deadline_label(program(apply_end_at=None, apply_period_raw='예산소진시까지'), now) == '마감 예산 소진 시'


def test_hostile_fields_and_unsafe_links_cannot_create_markup():
    item = program(title='<script>alert(1)</script> & "지원"', url='javascript:alert(1)')
    body = format_graded_message([(item, Assessment('A', '<b>검증 안 됨</b>', '미확인', EligibilityVerdict.CONFIRMED))], [], 1, 1)
    messages = messages_with_coverage(body, '<script>기관 메모</script>')
    rendered = [validate(message) for message in messages]
    text = ''.join(''.join(item.text) for item in rendered)
    assert '<script>alert(1)</script>' in text
    assert '<b>검증 안 됨</b>' in text
    assert '요건  확인 필요' in text
    assert '원문 링크 미확인' in text
    assert not any(item.hrefs for item in rendered)
    assert safe_url('https://user:password@example.com') == ''
    assert safe_url('https://example.com/a\n?token=x') == ''


def test_normal_cards_stay_together_and_last_item_and_coverage_are_preserved():
    items = [(program(title=f'제품 실증 지원 {i}', url=f'https://example.com/{i}'),
              Assessment('A', '실증 환경 제공 ' * 12, '충족', EligibilityVerdict.CONFIRMED)) for i in range(15)]
    body = format_graded_message(items, [], 40, 15)
    coverage = '수집 현황\n기업마당 행사: 일부 확인 · 100건 — 추가 공고 여부 확인 필요'
    messages = messages_with_coverage(body, coverage, max_length=1000)
    assert len(messages) > 1
    for message in messages:
        validate(message, 1000)
    for i in range(15):
        matches = [m for m in messages if f'제품 실증 지원 {i}</b>' in m]
        assert len(matches) == 1
        assert f'https://example.com/{i}"' in matches[0]
    assert '제품 실증 지원 14' in ''.join(messages)
    assert html.escape(coverage) in messages[-1]


def test_oversized_unicode_card_is_balanced_and_loses_no_tail():
    marker = '마지막핵심조건'
    reason = '😀 & 중요조건 ' * 1500 + marker
    body = format_graded_message([(program(), Assessment('B', reason, '미확인'))], [], 1, 1)
    messages = messages_with_coverage(body, '수집 현황', max_length=4000)
    text = ''.join(''.join(validate(message).text) for message in messages)
    assert reason in text
    assert marker in text
    assert sum(len(validate(message).hrefs) for message in messages) == 1


def test_oversized_source_link_is_visible_instead_of_silently_dropped():
    url = 'https://example.com/' + 'a' * 1200 + '?tail=last'
    messages = messages_with_coverage(f'<b>긴 원문 링크</b>\n<a href="{url}">원문 열기</a>', '', max_length=500)
    text = ''.join(''.join(validate(message, 500).text) for message in messages)
    assert url in text and '원문 열기' in text


def test_fallback_states_uncertainty_without_model_jargon_or_raw_scores():
    body = format_fallback_message([{'item': program(), 'score': 85, 'reasons': ['제조업 인증 지원']}])
    text = ''.join(validate(body).text)
    assert '상세 검토를 완료하지 못해' in text
    assert '제조업 인증 지원' in text
    assert 'LLM' not in text and '[85]' not in text
    assert '요건  확인 필요' in text


def test_coverage_keeps_failures_and_honest_uncertainty_without_false_cap_claim():
    coverage = (
        CoverageEntry('bizinfo_support', CollectionStatus.PARTIAL, None, 100, 100, 100,
                      'API 페이지 상한/반복 감지 — 전수 아님'),
        CoverageEntry('bizinfo_event', CollectionStatus.FAILED, None, 0, 0, 0, '연결 시간 초과'),
        CoverageEntry('sbiz24', CollectionStatus.SUCCESS, 497, 497, 497, 497),
    )
    text = format_coverage_manifest(coverage)
    assert 'coverage_manifest' not in text
    assert '기업마당 지원사업: 일부 확인' in text
    assert '추가 공고 여부 확인 필요' in text
    assert '수집 실패' in text and '연결 시간 초과' in text
    assert '497/497' in text
    assert '상한' not in text


def test_compatibility_formatter_never_truncates_content():
    body = '첫 공고\n' + '상세조건' * 2000 + '마지막공고'
    combined = with_coverage(body, '수집 현황')
    assert '마지막공고' in combined
    assert sum('마지막공고' in message for message in messages_with_coverage(combined, '')) == 1


def test_untrusted_markup_stays_text_and_invalid_unicode_url_is_rejected():
    messages = messages_with_coverage('<script>중요 조건</script><b>안내</b>', '')
    assert ''.join(validate(messages[0]).text) == '<script>중요 조건</script>안내'
    assert safe_url('https://example.com/\ud800') == ''
    assert safe_url('http://127.0.0.1/private') == ''
