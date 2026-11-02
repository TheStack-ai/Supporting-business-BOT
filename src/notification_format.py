"""Readable Telegram cards, with conservative HTML/UTF-16 message boundaries."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import html
from html.parser import HTMLParser
import ipaddress
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from src.llm_filter import Assessment
from src.program_state import ChangeKind

KST = ZoneInfo("Asia/Seoul")


def utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _text(value: object) -> str:
    text = str(value or "").encode("utf-8", "replace").decode("utf-8")
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def safe_url(value: object) -> str:
    value = str(value or "").strip()
    if not value or any(ord(char) < 33 or ord(char) == 127 or char.isspace()
                        or 0xD800 <= ord(char) <= 0xDFFF for char in value):
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            return ""
        if parsed.username is not None or parsed.password is not None:
            return ""
        if parsed.hostname.lower() == "localhost" or parsed.hostname.lower().endswith((".local", ".localhost")):
            return ""
        try:
            if not ipaddress.ip_address(parsed.hostname).is_global:
                return ""
        except ValueError:
            pass
        # Invalid ports are rejected rather than sent as unusable buttons.
        parsed.port
    except ValueError:
        return ""
    return value


def source_label(program: dict) -> str:
    labels = {
        "bizinfo": "기업마당", "fanfandaero": "판판대로",
        "sbiz24": "소상공인24", "sbiz24_combine": "소상공인24 통합",
    }
    return labels.get(program.get("source"), _text(program.get("source")) or "출처 미확인")


def change_marker(program: dict) -> str:
    kind = program.get("_change_kind")
    if kind == ChangeKind.NEW:
        return "신규"
    if kind != ChangeKind.CHANGED:
        return ""
    labels = {"title": "제목", "status": "상태", "apply_start_at": "접수 시작",
              "apply_end_at": "마감", "agency": "기관", "summary_raw": "내용"}
    changed = [labels.get(field, _text(field)) for field in program.get("_changed_fields", [])]
    return "변경 · " + (", ".join(changed) or "내용")


def _date(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        date = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return date.replace(tzinfo=KST) if date.tzinfo is None else date.astimezone(KST)


def deadline_label(program: dict, now: datetime | None = None) -> str:
    now = now or datetime.now(KST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=KST)
    now = now.astimezone(KST)
    value = program.get("apply_end_at")
    deadline = _date(value)
    if deadline:
        days = (deadline.date() - now.date()).days
        timing = "종료" if days < 0 else "오늘 마감" if days == 0 else f"D-{days}"
        display = deadline.strftime("%m/%d")
        if re.search(r"[T ]\d{2}:\d{2}", str(value)):
            display += deadline.strftime(" %H:%M")
        return f"마감 {display} · {timing}"
    raw = _text(program.get("apply_period_raw"))
    if "예산" in raw and "소진" in raw:
        return "마감 예산 소진 시"
    if program.get("status") == "상시" or "상시" in raw:
        return "마감 상시 접수"
    return "접수 마감 미확인"


def _event_period(program: dict) -> str:
    if program.get("kind") != "event":
        return ""
    start, end = _date(program.get("event_start_at")), _date(program.get("event_end_at"))
    if not start and not end:
        return ""
    if start and end and start.date() != end.date():
        return "행사 " + start.strftime("%m/%d") + "–" + end.strftime("%m/%d")
    return "행사 " + (start or end).strftime("%m/%d")


def _link(program: dict) -> str:
    url = safe_url(program.get("url"))
    return f'<a href="{html.escape(url, quote=True)}">공고 원문 열기 ↗</a>' if url else "원문 링크 미확인"


def _card(program: dict, assessment: Assessment, index: int) -> str:
    title = _text(program.get("title")) or "제목 미확인"
    kind = "행사" if program.get("kind") == "event" else "지원사업"
    priority = "우선 검토" if assessment.grade == "A" else "요건 확인"
    metadata = " · ".join([priority, kind, source_label(program)])
    fit = _text(getattr(assessment, "fit_reason", "")) or _text(assessment.reason)
    verdict = _text(getattr(assessment.verdict, "value", assessment.verdict))
    if verdict == "확인됨" and assessment.eligibility != "충족":
        verdict = "확인 필요"
    missing = tuple(_text(value) for value in getattr(assessment, "missing_requirements", ()) if _text(value))
    lines = [f"<b>{index}. {html.escape(title)}</b>", html.escape(metadata),
             html.escape(deadline_label(program))]
    if program.get("_change_kind") == ChangeKind.CHANGED:
        lines.insert(2, html.escape(change_marker(program)))
    if period := _event_period(program):
        lines.append(html.escape(period))
    if fit:
        lines.append("활용  " + html.escape(fit))
    lines.append("요건  " + html.escape(verdict or "확인 필요"))
    if missing:
        lines.append("확인할 점  " + html.escape(" · ".join(missing)))
    elif verdict != "확인됨":
        lines.append("확인할 점  상세 신청자격 확인")
    lines.append(_link(program))
    return "\n".join(lines)


def format_graded_message(grade_a: list[tuple[dict, Assessment]], grade_b: list[tuple[dict, Assessment]],
                          total_checked: int, stage1_passed: int) -> str:
    timestamp = datetime.now(KST).strftime("%m/%d %H:%M KST")
    header = f"<b>싸이베리어 · 지원사업·행사</b>\n{timestamp}"
    if not grade_a and not grade_b:
        return header + f"\n\n이번 회차에 새로 추천할 공고가 없습니다.\n{total_checked}건 검토 · {stage1_passed}건 상세 확인"
    summary = " · ".join(label for label in [f"우선 검토 {len(grade_a)}건" if grade_a else "",
                                            f"요건 확인 {len(grade_b)}건" if grade_b else ""] if label)
    parts = [header + "\n" + summary]
    index = 1
    for pairs in (grade_a, grade_b):
        if pairs:
            for program, assessment in pairs:
                parts.append(_card(program, assessment, index))
                index += 1
    return "\n\n".join(parts)


def format_fallback_message(recommendations: list[dict]) -> str:
    timestamp = datetime.now(KST).strftime("%m/%d %H:%M KST")
    parts = [f"<b>싸이베리어 · 요건 확인</b>\n{timestamp}\n"
             "상세 검토를 완료하지 못해 기본 사업 조건으로 추렸습니다."]
    if not recommendations:
        parts.append("이번 회차에 새로 추천할 공고가 없습니다.")
    for index, recommendation in enumerate(recommendations, 1):
        program = recommendation["item"]
        fit = (_text(recommendation.get("fit_reason")) or _text(program.get("_fit_reason"))
               or " · ".join(_text(reason) for reason in recommendation.get("reasons", []) if _text(reason)))
        assessment = Assessment("B", fit or "관련 사업 여부 확인", "미확인")
        parts.append(_card(program, assessment, index))
    return "\n\n".join(parts)


@dataclass(frozen=True)
class _Token:
    kind: str
    value: str
    tag: str = ""


class _SafeHTML(HTMLParser):
    """Canonicalize only our Telegram tags; unknown markup remains visible text."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tokens: list[_Token] = []
        self.stack: list[tuple[str, str]] = []

    def handle_data(self, data):
        if data:
            self.tokens.append(_Token("text", data))

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.handle_data("\n")
            return
        canonical = {"strong": "b", "em": "i"}.get(tag, tag)
        if canonical in ("b", "i", "a") and len(self.stack) < 8:
            if canonical == "a":
                url = safe_url(dict(attrs).get("href"))
                if not url:
                    self.stack.append((tag, ""))
                    return
                opening = f'<a href="{html.escape(url, quote=True)}">'
            else:
                opening = f"<{canonical}>"
            self.tokens.append(_Token("open", opening, canonical))
            self.stack.append((tag, canonical))
        else:
            self.handle_data(self.get_starttag_text() or f"<{tag}>")
            self.stack.append((tag, ""))

    def handle_endtag(self, tag):
        found = next((i for i in range(len(self.stack)-1, -1, -1) if self.stack[i][0] == tag), None)
        if found is None:
            self.handle_data(f"</{tag}>")
            return
        for original, canonical in reversed(self.stack[found:]):
            if canonical:
                self.tokens.append(_Token("close", f"</{canonical}>", canonical))
            elif original not in ("b", "i", "strong", "em", "a"):
                self.handle_data(f"</{original}>")
        del self.stack[found:]

    def handle_startendtag(self, tag, attrs):
        if tag == "br":
            self.handle_data("\n")
        else:
            self.handle_data(self.get_starttag_text() or f"<{tag}/>")

    def finish(self):
        for _original, canonical in reversed(self.stack):
            if canonical:
                self.tokens.append(_Token("close", f"</{canonical}>", canonical))
        self.stack.clear()
        return self.tokens


def _encoded(token: _Token) -> str:
    return html.escape(token.value, quote=False) if token.kind == "text" else token.value


def _blocks(tokens: list[_Token]) -> list[list[_Token]]:
    blocks, current, depth = [], [], 0
    for token in tokens:
        if token.kind == "text" and depth == 0:
            for part in re.split(r"(\n{2,})", token.value):
                if part.startswith("\n\n"):
                    if current:
                        blocks.append(current); current = []
                elif part:
                    current.append(_Token("text", part))
        else:
            current.append(token)
            depth += 1 if token.kind == "open" else -1 if token.kind == "close" else 0
    if current:
        blocks.append(current)
    return blocks


def _oversized(tokens: list[_Token], limit: int) -> list[str]:
    """Split exceptional long cards with balanced/reopened tags, losing no text."""
    output: list[str] = []
    active: list[_Token] = []
    current = ""
    ignored_links = 0

    def closing() -> str:
        return "".join(f"</{token.tag}>" for token in reversed(active))

    def flush():
        nonlocal current
        if current:
            output.append(current + closing())
        current = "".join(token.value for token in active)

    def add_text(text: str):
        nonlocal current
        for char in text:
            encoded = html.escape(char, quote=False)
            if utf16_length(current + encoded + closing()) > limit:
                flush()
            if utf16_length(current + encoded + closing()) > limit:
                raise ValueError("formatting overhead exceeds message budget")
            current += encoded

    for token in tokens:
        if token.kind == "text":
            add_text(token.value)
        elif token.kind == "open":
            overhead = "".join(item.value for item in active) + token.value + f"</{token.tag}>" + closing()
            if utf16_length(overhead) > limit - 16:
                # An unusually long URL cannot fit a Telegram anchor. Preserve
                # the URL as visible continuation text instead of silently dropping it.
                if token.tag == "a":
                    match = re.search(r'href="(.*)"', token.value)
                    add_text("원문 주소: " + html.unescape(match.group(1) if match else "") + "\n")
                    ignored_links += 1
                    continue
                raise ValueError("formatting overhead exceeds message budget")
            if utf16_length(current + token.value + f"</{token.tag}>" + closing()) > limit:
                flush()
            current += token.value
            active.append(token)
        else:
            if token.tag == "a" and ignored_links:
                ignored_links -= 1
                continue
            current += token.value
            if active:
                active.pop()
    if current:
        output.append(current)
    return output


def _split_message(body: str, max_length: int) -> list[str]:
    if max_length < 64:
        raise ValueError("message limit must be at least 64 UTF-16 units")
    parser = _SafeHTML(); parser.feed(body.strip())
    blocks = _blocks(parser.finish())
    messages, current = [], ""
    for block in blocks:
        rendered = "".join(_encoded(token) for token in block).strip()
        if not rendered:
            continue
        pieces = [rendered] if utf16_length(rendered) <= max_length else _oversized(block, max_length)
        for piece in pieces:
            candidate = current + ("\n\n" if current else "") + piece
            if utf16_length(candidate) <= max_length:
                current = candidate
            else:
                if current:
                    messages.append(current)
                current = piece
    if current:
        messages.append(current)
    return messages


def messages_with_coverage(body: str, coverage_manifest: str, max_length: int = 4000) -> tuple[str, ...]:
    combined = body.strip()
    if coverage_manifest.strip():
        combined += ("\n\n" if combined else "") + html.escape(coverage_manifest.strip())
    return tuple(_split_message(combined, max_length))


def with_coverage(body: str, coverage_manifest: str) -> str:
    """Compatibility string formatter; use messages_with_coverage before sending."""
    return body.rstrip() + ("\n\n" + html.escape(coverage_manifest.strip()) if coverage_manifest.strip() else "")
