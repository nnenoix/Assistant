"""Unit tests for Phase 10 external wrappers: web, fx_rate, pdf_gen, vision, translation."""
from unittest.mock import MagicMock, patch

import pytest


# ============ web.fetch / web.search ============

def test_web_fetch_text_mode_strips_html():
    from src.tools import web

    fake_resp = MagicMock()
    fake_resp.headers = {"Content-Type": "text/html; charset=utf-8"}
    fake_resp.url = "https://example.com/"
    fake_resp.status_code = 200
    html = b"<html><body><script>bad();</script><p>Hello world</p><style>a{}</style></body></html>"
    fake_resp.iter_content.return_value = iter([html])
    with patch("src.tools.web.retrying_request", return_value=fake_resp):
        result = web.fetch("https://example.com/")
    assert "Hello world" in result["content"]
    assert "bad()" not in result["content"]
    assert "{}" not in result["content"]
    assert result["_meta"]["status_code"] == 200


def test_web_fetch_json_mode_parses():
    from src.tools import web

    fake_resp = MagicMock()
    fake_resp.headers = {"Content-Type": "application/json"}
    fake_resp.url = "https://api.example.com/v1"
    fake_resp.status_code = 200
    fake_resp.iter_content.return_value = iter([b'{"name": "test", "n": 42}'])
    with patch("src.tools.web.retrying_request", return_value=fake_resp):
        result = web.fetch("https://api.example.com/v1", mode="json")
    assert result["content"] == {"name": "test", "n": 42}


def test_web_fetch_json_invalid_returns_error():
    from src.tools import web

    fake_resp = MagicMock()
    fake_resp.headers = {"Content-Type": "application/json"}
    fake_resp.url = "https://x/"
    fake_resp.status_code = 200
    fake_resp.iter_content.return_value = iter([b"not json"])
    with patch("src.tools.web.retrying_request", return_value=fake_resp):
        result = web.fetch("https://x/", mode="json")
    assert result["content"] is None
    assert "json parse error" in result["_meta"]["error"]


def test_web_fetch_truncates_at_cap():
    from src.tools import web

    fake_resp = MagicMock()
    fake_resp.headers = {"Content-Type": "text/html"}
    fake_resp.url = "https://big/"
    fake_resp.status_code = 200
    # Stream that exceeds 1MB
    big_chunk = b"<p>x</p>" * 200_000  # ~1.6MB
    fake_resp.iter_content.return_value = iter([big_chunk])
    with patch("src.tools.web.retrying_request", return_value=fake_resp):
        result = web.fetch("https://big/", mode="html")
    assert result["_meta"]["truncated"] is True


def test_web_fetch_rejects_unknown_mode():
    from src.tools import web
    with pytest.raises(ValueError, match="unknown mode"):
        web.fetch("https://x/", mode="binary")


# ============ external.fx_rate / external.open_url ============

def test_fx_rate_parses_cbr_xml():
    """Mock CBR's XML response and verify rate extraction."""
    from src.tools import external

    # Minimal CBR XML format (encoded to windows-1251 to match CBR)
    xml_str = """<?xml version="1.0" encoding="windows-1251"?>
<ValCurs Date="20.05.2026" name="Foreign Currency Market">
<Valute ID="R01235"><NumCode>840</NumCode><CharCode>USD</CharCode>
<Nominal>1</Nominal><Name>Доллар США</Name><Value>89,1234</Value></Valute>
<Valute ID="R01239"><NumCode>978</NumCode><CharCode>EUR</CharCode>
<Nominal>1</Nominal><Name>Евро</Name><Value>96,5432</Value></Valute>
</ValCurs>"""
    xml = xml_str.encode("windows-1251")
    fake_resp = MagicMock()
    fake_resp.content = xml
    fake_resp.raise_for_status = MagicMock()
    with patch("src.tools.external.retrying_request", return_value=fake_resp):
        result = external.fx_rate("USD", date_iso="2026-05-20")
    assert result["currency"] == "USD"
    assert abs(result["rate_to_rub"] - 89.1234) < 1e-6
    assert result["nominal"] == 1


def test_fx_rate_unknown_currency_returns_error():
    from src.tools import external

    xml = (
        '<?xml version="1.0" encoding="windows-1251"?>\n'
        '<ValCurs Date="20.05.2026"><Valute><CharCode>USD</CharCode><Nominal>1</Nominal><Value>89,00</Value></Valute></ValCurs>'
    ).encode("windows-1251")
    fake_resp = MagicMock()
    fake_resp.content = xml
    fake_resp.raise_for_status = MagicMock()
    with patch("src.tools.external.retrying_request", return_value=fake_resp):
        result = external.fx_rate("XYZ")
    assert result["rate_to_rub"] is None
    assert "not found" in result["_meta"]["error"]


def test_fx_rate_rejects_bad_date():
    from src.tools import external
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        external.fx_rate("USD", date_iso="20-may-2026")


def test_open_url_dispatches_to_platform():
    from src.tools import external

    with patch("src.tools.external.sys.platform", "linux"), \
         patch("src.tools.external.webbrowser.open") as mock_open:
        result = external.open_url("https://example.com")
    assert result["ok"]
    mock_open.assert_called_once_with("https://example.com", new=2)


# ============ pdf_gen ============

def test_pdf_create_text_produces_pdf(tmp_path):
    from src.tools import pdf_gen
    dest = tmp_path / "out.pdf"
    result = pdf_gen.create_pdf(
        "Привет, мир.\n\nВторой абзац с цифрами 123.",
        str(dest),
        kind="text",
        title="Test Report",
    )
    assert result["ok"]
    assert dest.exists()
    assert dest.read_bytes()[:5] == b"%PDF-"


def test_pdf_create_table(tmp_path):
    from src.tools import pdf_gen
    dest = tmp_path / "table.pdf"
    result = pdf_gen.create_pdf(
        {"headers": ["Бренд", "Прибыль ₽"], "rows": [
            ["IdealNight", 3087967], ["SensesAura", -285280],
        ]},
        str(dest),
        kind="table",
        title="Q1 by brand",
    )
    assert result["ok"]
    assert dest.read_bytes()[:5] == b"%PDF-"


def test_pdf_create_report_with_sections(tmp_path):
    from src.tools import pdf_gen
    dest = tmp_path / "report.pdf"
    result = pdf_gen.create_pdf(
        {
            "title": "Q1 2026 Report",
            "sections": [
                {"heading": "Резюме", "paragraphs": ["Цифры неплохие.", "Альтер Хим в нуле."]},
                {"heading": "По брендам", "table": {"headers": ["X", "Y"], "rows": [["a", 1], ["b", 2]]}},
            ],
        },
        str(dest),
        kind="report",
    )
    assert result["ok"]


def test_pdf_create_rejects_unknown_kind(tmp_path):
    from src.tools import pdf_gen
    with pytest.raises(ValueError, match="unknown kind"):
        pdf_gen.create_pdf("x", str(tmp_path / "x.pdf"), kind="3d_holographic")


# ============ vision ============

def test_vision_probe_returns_struct():
    from src.tools import vision
    result = vision.probe()
    assert "available" in result
    assert "info" in result


def test_vision_ocr_graceful_when_tesseract_missing():
    """If pytesseract isn't installed, ocr() returns an error dict, not raises."""
    from src.tools import vision

    # Simulate import failure
    with patch.object(vision, "_check_tesseract", return_value=(False, "not installed")):
        result = vision.ocr("nonexistent.png")
    assert result["text"] is None
    assert result["_meta"]["tesseract_available"] is False


# ============ translation ============

def test_translate_returns_error_when_argos_missing():
    """When argostranslate isn't installed, translate() returns an error
    dict rather than raising."""
    from src.tools import translation
    with patch.object(translation, "_check_argos", return_value=(False, "not installed")):
        result = translation.translate("Hello", "ru")
    assert result["translated"] is None
    assert result["_meta"]["argos_available"] is False


def test_translate_probe_returns_struct():
    """Already covered above, but as sanity check on the import chain."""
    from src.tools import translation
    result = translation.probe()
    assert result["available"] in (True, False)
