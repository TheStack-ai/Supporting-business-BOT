from src.detail_crawler import extract_text_from_html, fetch_detail


def test_extract_text_strips_html():
    html = """
    <html><head><title>Test</title></head>
    <body>
    <div id="header">Navigation stuff</div>
    <div class="content">
        <h2>지원대상</h2>
        <p>중소기업 중 제조업을 영위하는 기업</p>
        <h2>지원내용</h2>
        <p>최대 1억원 지원</p>
    </div>
    <div id="footer">Copyright 2026</div>
    </body></html>
    """
    text = extract_text_from_html(html)
    assert "중소기업 중 제조업을 영위하는 기업" in text
    assert "최대 1억원 지원" in text
    assert "<div" not in text
    assert "<p>" not in text


def test_extract_text_max_length():
    html = "<html><body>" + "가" * 20000 + "</body></html>"
    text = extract_text_from_html(html)
    assert len(text) <= 8000


def test_extract_text_empty_html():
    assert extract_text_from_html("") == ""
    assert extract_text_from_html(None) == ""


def test_fetch_detail_network_error(monkeypatch):
    """Network failure should return empty string, not raise."""
    import requests as req

    def mock_get(*args, **kwargs):
        raise req.ConnectionError("mocked connection failure")

    monkeypatch.setattr(req, "get", mock_get)
    result = fetch_detail("https://www.bizinfo.go.kr/fake")
    assert result == ""
