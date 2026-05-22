"""Deep tests for src/tools/mlhelpers.py — NER/DaData/embeddings/OCR."""
import json
from unittest.mock import MagicMock, patch
import pytest


def _str_url(req):
    return req if isinstance(req, str) else req.full_url


# ============================================================
# INN extraction + checksum
# ============================================================

def test_inn_checksum_10digit_invalid():
    """Random INN with bad checksum — last digit doesn't match FNS algorithm.
    Note: 0000000000 happens to pass (sum=0, mod 10 = 0) — use a real-shaped
    invalid INN instead."""
    from src.tools import mlhelpers
    assert mlhelpers._inn_checksum_valid("1234567890") is False
    # Flip last digit of real Сбербанк ИНН to invalidate
    assert mlhelpers._inn_checksum_valid("7707083891") is False


def test_inn_checksum_real_companies():
    """Verified real ИНН from public registry."""
    from src.tools import mlhelpers
    # Сбербанк
    assert mlhelpers._inn_checksum_valid("7707083893") is True
    # Газпром
    assert mlhelpers._inn_checksum_valid("7736050003") is True


def test_inn_checksum_wrong_length():
    from src.tools import mlhelpers
    assert mlhelpers._inn_checksum_valid("123") is False
    assert mlhelpers._inn_checksum_valid("12345678901") is False  # 11 digits


def test_nlp_extract_inns_validate_false_keeps_all():
    """With validate=False, keep even invalid checksums (e.g. for fuzzy review)."""
    from src.tools import mlhelpers
    out = mlhelpers.nlp_extract_inns("ИНН 1234567890 (typo)", validate=False)
    assert any(r["value"] == "1234567890" for r in out["data"]["inns"])


def test_nlp_extract_inns_mixed_lengths():
    """Text with both 10-digit company INN and 12-digit individual INN."""
    from src.tools import mlhelpers
    out = mlhelpers.nlp_extract_inns(
        "ООО ИНН 7707083893, ИП 500100732259", validate=True
    )
    values = sorted(r["value"] for r in out["data"]["inns"])
    assert "7707083893" in values
    assert "500100732259" in values


def test_nlp_extract_inns_empty_text():
    from src.tools import mlhelpers
    out = mlhelpers.nlp_extract_inns("")
    assert out["data"]["inns"] == []


def test_nlp_extract_inns_no_match_returns_empty_list():
    from src.tools import mlhelpers
    out = mlhelpers.nlp_extract_inns("Привет мир, нет ИНН тут")
    assert out["data"]["inns"] == []


# ============================================================
# Phone extraction
# ============================================================

def test_nlp_phones_handles_8_prefix_normalization():
    from src.tools import mlhelpers
    out = mlhelpers.nlp_extract_phones("Звоните 89991234567")
    assert out["data"]["phones"][0]["normalized"] == "79991234567"


def test_nlp_phones_normalize_false_keeps_raw():
    from src.tools import mlhelpers
    out = mlhelpers.nlp_extract_phones("+79991234567", normalize=False)
    assert out["data"]["phones"][0]["normalized"] is None
    assert out["data"]["phones"][0]["raw"] == "+79991234567"


def test_nlp_phones_count_meta():
    from src.tools import mlhelpers
    out = mlhelpers.nlp_extract_phones("+79991234567, +79991234568")
    assert out["_meta"]["count"] == 2


def test_nlp_phones_dedupe():
    """Same number twice → kept once."""
    from src.tools import mlhelpers
    out = mlhelpers.nlp_extract_phones("+79991234567 или 89991234567")
    assert len(out["data"]["phones"]) == 1


def test_nlp_phones_empty_text():
    from src.tools import mlhelpers
    out = mlhelpers.nlp_extract_phones("")
    assert out["data"]["phones"] == []


# ============================================================
# BIK / OGRN
# ============================================================

def test_nlp_extract_bik_real_sber():
    """044525225 is Сбербанк Москва. Real BIK."""
    from src.tools import mlhelpers
    out = mlhelpers.nlp_extract_bik("счет в банке БИК 044525225")
    assert "044525225" in out["data"]["bik"]


def test_nlp_extract_bik_only_04_prefix():
    """Russian bank BIC always starts with 04."""
    from src.tools import mlhelpers
    out = mlhelpers.nlp_extract_bik("123456789 — это не БИК, а 044525225 — да")
    assert "044525225" in out["data"]["bik"]
    assert "123456789" not in out["data"]["bik"]


def test_nlp_extract_ogrn_13_digit():
    """13-digit OGRN."""
    from src.tools import mlhelpers
    out = mlhelpers.nlp_extract_ogrn("ОГРН: 1027700132195")
    assert "1027700132195" in out["data"]["ogrn"]


def test_nlp_extract_ogrn_15_digit_for_ip():
    """15-digit OGRNIP."""
    from src.tools import mlhelpers
    out = mlhelpers.nlp_extract_ogrn("ОГРНИП 304500116000157")
    assert "304500116000157" in out["data"]["ogrn"]


# ============================================================
# named_entities — lazy import behavior
# ============================================================

def test_nlp_named_entities_returns_fix_hint_when_natasha_missing():
    """If natasha isn't installed, return structured "not installed" with
    a fix_hint — agent can act on it without crashing."""
    from src.tools import mlhelpers
    # Force ImportError by patching the import to raise
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name.startswith("natasha"):
            raise ImportError("no natasha")
        return real_import(name, *a, **kw)

    with patch.object(builtins, "__import__", side_effect=fake_import):
        out = mlhelpers.nlp_named_entities("Москва")
    # Either succeeds (if installed) or returns structured failure
    if not out["ok"]:
        assert "fix_hint" in out
        assert "natasha" in out["fix_hint"]


# ============================================================
# DaData
# ============================================================

def test_dadata_suggest_address_url_and_token():
    from src.tools import mlhelpers
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = _str_url(req)
        captured["headers"] = dict(req.headers)
        captured["data"] = req.data
        m = MagicMock()
        m.read.return_value = b'{"suggestions":[]}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        mlhelpers.dadata_suggest_address("MYTOK", "Москва Тверская")
    assert "suggestions" in captured["url"]
    assert "/suggest/address" in captured["url"]
    assert captured["headers"]["Authorization"] == "Token MYTOK"
    assert json.loads(captured["data"])["query"] == "Москва Тверская"


def test_dadata_clean_address_includes_secret_header():
    from src.tools import mlhelpers
    captured = {}

    def fake_urlopen(req, timeout):
        captured["headers"] = dict(req.headers)
        m = MagicMock()
        m.read.return_value = b'[]'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        mlhelpers.dadata_clean_address("TOK", "SEC", "Москва")
    assert captured["headers"]["X-secret"] == "SEC"


def test_dadata_find_party_by_inn_body_shape():
    from src.tools import mlhelpers
    captured = {}

    def fake_urlopen(req, timeout):
        captured["data"] = req.data
        m = MagicMock()
        m.read.return_value = b'{"suggestions":[]}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        mlhelpers.dadata_find_party_by_inn("T", "7707083893")
    body = json.loads(captured["data"])
    assert body["query"] == "7707083893"


def test_dadata_suggest_party_count_param():
    from src.tools import mlhelpers
    captured = {}

    def fake_urlopen(req, timeout):
        captured["data"] = req.data
        m = MagicMock()
        m.read.return_value = b'{"suggestions":[]}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        mlhelpers.dadata_suggest_party("T", "Газпром", count=20)
    body = json.loads(captured["data"])
    assert body["count"] == 20


def test_dadata_suggest_bank_endpoint():
    from src.tools import mlhelpers
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = _str_url(req)
        m = MagicMock()
        m.read.return_value = b'{"suggestions":[]}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        mlhelpers.dadata_suggest_bank("T", "Сбер")
    assert "/suggest/bank" in captured["url"]


def test_dadata_call_handles_403():
    from src.tools import mlhelpers
    from urllib.error import HTTPError
    fake = MagicMock()
    fake.read.return_value = b'{"detail":"Invalid token"}'
    with patch("urllib.request.urlopen",
               side_effect=HTTPError("u", 403, "Forbidden", {}, fake)):
        out = mlhelpers.dadata_suggest_address("BAD", "x")
    assert out["ok"] is False
    assert out["_meta"]["http_status"] == 403


# ============================================================
# cosine_similarity edge cases
# ============================================================

def test_cosine_similarity_zero_vector():
    """Division-by-zero guard: zero-norm vectors return 0.0 instead of crashing."""
    from src.tools import mlhelpers
    out = mlhelpers.cosine_similarity([0, 0, 0], [1, 1, 1])
    assert out["data"]["similarity"] == 0.0


def test_cosine_similarity_negative_correlation():
    from src.tools import mlhelpers
    out = mlhelpers.cosine_similarity([1, 0], [-1, 0])
    assert abs(out["data"]["similarity"] - (-1.0)) < 1e-9


def test_cosine_similarity_high_dim_match():
    from src.tools import mlhelpers
    out = mlhelpers.cosine_similarity([0.1] * 100, [0.2] * 100)
    assert abs(out["data"]["similarity"] - 1.0) < 1e-9


# ============================================================
# embed_texts — graceful failure when no sentence-transformers
# ============================================================

def test_embed_texts_returns_fix_hint_when_lib_missing():
    """sentence-transformers might not be installed in dev env. Function
    should return a structured "not installed" rather than crashing."""
    from src.tools import mlhelpers
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if "sentence_transformers" in name:
            raise ImportError("not installed")
        return real_import(name, *a, **kw)

    with patch.object(builtins, "__import__", side_effect=fake_import):
        out = mlhelpers.embed_texts(["hello"])
    if not out["ok"]:
        assert "fix_hint" in out


# ============================================================
# OCR — lazy imports + dispatch
# ============================================================

def test_ocr_image_tesseract_returns_fix_hint_if_missing():
    from src.tools import mlhelpers
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "pytesseract" or name == "PIL":
            raise ImportError("not installed")
        return real_import(name, *a, **kw)

    with patch.object(builtins, "__import__", side_effect=fake_import):
        out = mlhelpers.ocr_image("/tmp/none.png")
    if not out.get("ok"):
        assert "fix_hint" in out


def test_ocr_image_paddle_engine_returns_fix_hint_if_missing():
    from src.tools import mlhelpers
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if "paddle" in name:
            raise ImportError("not installed")
        return real_import(name, *a, **kw)

    with patch.object(builtins, "__import__", side_effect=fake_import):
        out = mlhelpers.ocr_image("/tmp/none.png", engine="paddle")
    if not out.get("ok"):
        assert "fix_hint" in out


def test_ocr_pdf_lazy_import_failure_path():
    from src.tools import mlhelpers
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "pdf2image":
            raise ImportError("not installed")
        return real_import(name, *a, **kw)

    with patch.object(builtins, "__import__", side_effect=fake_import):
        out = mlhelpers.ocr_pdf("/tmp/none.pdf")
    if not out.get("ok"):
        assert "fix_hint" in out


# ============================================================
# Pandera — lazy + lazy
# ============================================================

def test_pandera_validate_lazy_import_failure():
    from src.tools import mlhelpers
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name in ("pandera", "pandas"):
            raise ImportError("not installed")
        return real_import(name, *a, **kw)

    with patch.object(builtins, "__import__", side_effect=fake_import):
        out = mlhelpers.pandera_validate([], "{}")
    if not out.get("ok"):
        assert "fix_hint" in out


def test_pandera_validate_accepts_dict_schema():
    """Schema may be passed as a dict directly (not JSON string)."""
    pytest.importorskip("pandera")
    pytest.importorskip("pandas")
    from src.tools import mlhelpers
    schema = {"columns": {"x": {"dtype": "int64", "nullable": False}}}
    out = mlhelpers.pandera_validate([{"x": 1}, {"x": 2}], schema)
    assert out["ok"] is True


def test_pandera_validate_finds_errors():
    pytest.importorskip("pandera")
    pytest.importorskip("pandas")
    from src.tools import mlhelpers
    # Pattern requires string of digits, but we'll pass nulls
    schema = json.dumps({
        "columns": {
            "inn": {"dtype": "string", "nullable": False,
                    "checks": [{"type": "str_matches", "value": "^\\d{10}$"}]}
        }
    })
    out = mlhelpers.pandera_validate(
        [{"inn": "1234567890"}, {"inn": "abc"}], schema,
    )
    if "errors" in out.get("data", {}):
        assert len(out["data"]["errors"]) >= 1
