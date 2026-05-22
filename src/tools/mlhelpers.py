"""ML / rules wrappers for deterministic Russian text processing.

These are the "hands" layer the audit calls out — without them the LLM
keeps re-inventing the same NER / OCR / similarity rules per session.
External libs (natasha, dadata, sentence-transformers, paddleocr,
pandera) are imported LAZILY so missing installs don't break the
registry at load time — they error only when the tool is actually called.

All return uniform shape: {ok, data | result, error?, _meta}.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


# ============================================================
# Natasha NER (Russian) — INN / KPP / БИК / phone / org / person / location
# ============================================================

# Cheap heuristic fallbacks so the tool works even without natasha installed.
_INN_RE_10 = re.compile(r"\b\d{10}\b")
_INN_RE_12 = re.compile(r"\b\d{12}\b")
_KPP_RE = re.compile(r"\b\d{9}\b")
_BIK_RE = re.compile(r"\b04\d{7}\b")  # Russian bank BIC always starts with 04
_PHONE_RE = re.compile(r"(?:\+7|8)[\s\-\(\)]?\d{3}[\s\-\(\)]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}")
_OGRN_RE = re.compile(r"\b\d{13}\b|\b\d{15}\b")


def _inn_checksum_valid(inn: str) -> bool:
    """Validate Russian ИНН 10 or 12 digit checksum (FNS algorithm)."""
    if len(inn) == 10:
        w = [2, 4, 10, 3, 5, 9, 4, 6, 8]
        s = sum(int(inn[i]) * w[i] for i in range(9)) % 11 % 10
        return s == int(inn[9])
    if len(inn) == 12:
        w1 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
        w2 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
        s1 = sum(int(inn[i]) * w1[i] for i in range(10)) % 11 % 10
        s2 = sum(int(inn[i]) * w2[i] for i in range(11)) % 11 % 10
        return s1 == int(inn[10]) and s2 == int(inn[11])
    return False


def nlp_extract_inns(text: str, validate: bool = True) -> dict:
    """Extract Russian INNs from text. `validate=True` keeps only valid
    checksums. Returns {inns: [{value, length, valid}]}."""
    seen: dict[str, dict] = {}
    for m in list(_INN_RE_10.finditer(text)) + list(_INN_RE_12.finditer(text)):
        v = m.group(0)
        if v in seen:
            continue
        valid = _inn_checksum_valid(v)
        if validate and not valid:
            continue
        seen[v] = {"value": v, "length": len(v), "valid": valid}
    return {"ok": True, "data": {"inns": list(seen.values())}, "_meta": {"input_len": len(text)}}


def nlp_extract_phones(text: str, normalize: bool = True) -> dict:
    """Extract Russian phone numbers. `normalize=True` strips formatting →
    E.164-like `79991234567`."""
    raw = [m.group(0) for m in _PHONE_RE.finditer(text)]
    out: list[dict] = []
    seen = set()
    for r in raw:
        digits = re.sub(r"\D", "", r)
        if digits.startswith("8") and len(digits) == 11:
            digits = "7" + digits[1:]
        if len(digits) == 11 and digits not in seen:
            seen.add(digits)
            out.append({"raw": r, "normalized": digits if normalize else None})
    return {"ok": True, "data": {"phones": out}, "_meta": {"count": len(out)}}


def nlp_extract_bik(text: str) -> dict:
    """Extract Russian bank BIC codes (start with 04, 9 digits)."""
    found = list(dict.fromkeys(_BIK_RE.findall(text)))
    return {"ok": True, "data": {"bik": found}}


def nlp_extract_ogrn(text: str) -> dict:
    """Extract Russian OGRN/OGRNIP (13 or 15 digits)."""
    found = list(dict.fromkeys(_OGRN_RE.findall(text)))
    return {"ok": True, "data": {"ogrn": found}}


def nlp_named_entities(text: str) -> dict:
    """Full Natasha NER pass (org / person / location). Lazy-imports natasha.
    On missing install, falls back to a stub with a hint."""
    try:
        from natasha import (
            Segmenter, MorphVocab, NewsEmbedding,
            NewsMorphTagger, NewsSyntaxParser, NewsNERTagger,
            Doc,
        )
    except ImportError:
        return {"ok": False, "error": "natasha not installed",
                "fix_hint": "pip install natasha", "_meta": {}}
    seg = Segmenter()
    mv = MorphVocab()
    emb = NewsEmbedding()
    morph = NewsMorphTagger(emb)
    syntax = NewsSyntaxParser(emb)
    ner = NewsNERTagger(emb)
    doc = Doc(text)
    doc.segment(seg)
    doc.tag_morph(morph)
    doc.parse_syntax(syntax)
    doc.tag_ner(ner)
    spans = []
    for s in doc.spans:
        spans.append({"text": s.text, "type": s.type, "start": s.start, "stop": s.stop})
    return {"ok": True, "data": {"spans": spans}, "_meta": {"count": len(spans)}}


# ============================================================
# DaData — address / company / bank normalization
# ============================================================

_DADATA_BASE = "https://suggestions.dadata.ru/suggestions/api/4_1/rs"
_DADATA_CLEAN = "https://cleaner.dadata.ru/api/v1/clean"


def _dadata_post(url: str, token: str, body: Any, secret: str | None = None,
                 timeout: int = 30) -> dict:
    h = {
        "Content-Type": "application/json", "Accept": "application/json",
        "Authorization": f"Token {token}",
    }
    if secret:
        h["X-Secret"] = secret
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 method="POST", headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"ok": True, "data": json.loads(resp.read().decode("utf-8")),
                    "_meta": {"http_status": resp.status}}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": e.read()[:300].decode("utf-8", errors="replace"),
                "_meta": {"http_status": e.code}}


def dadata_suggest_address(token: str, query: str, count: int = 10) -> dict:
    """Address autocomplete (КЛАДР/ФИАС-backed). Use for «улица Ленина 10 → точная нормализованная»."""
    return _dadata_post(f"{_DADATA_BASE}/suggest/address", token,
                        {"query": query, "count": count})


def dadata_clean_address(token: str, secret: str, address: str) -> dict:
    """Full address cleaning + geocoding (paid endpoint)."""
    return _dadata_post(f"{_DADATA_CLEAN}/address", token, [address], secret=secret)


def dadata_suggest_party(token: str, query: str, count: int = 10) -> dict:
    """Company / IP autocomplete by name or INN. Returns full FNS data."""
    return _dadata_post(f"{_DADATA_BASE}/suggest/party", token,
                        {"query": query, "count": count})


def dadata_find_party_by_inn(token: str, inn: str) -> dict:
    """Lookup a company / IP by exact INN."""
    return _dadata_post(f"{_DADATA_BASE}/findById/party", token, {"query": inn})


def dadata_suggest_bank(token: str, query: str, count: int = 10) -> dict:
    """Bank autocomplete by name or BIC."""
    return _dadata_post(f"{_DADATA_BASE}/suggest/bank", token,
                        {"query": query, "count": count})


# ============================================================
# Sentence embeddings (semantic similarity)
# ============================================================

def embed_texts(texts: list[str], model: str = "intfloat/multilingual-e5-small") -> dict:
    """Embed `texts` with `model`. Default = multilingual-e5-small (good for RU+EN,
    384-dim). Lazy-imports sentence-transformers."""
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        return {"ok": False, "error": "sentence-transformers not installed",
                "fix_hint": "pip install sentence-transformers", "_meta": {}}
    m = SentenceTransformer(model)
    vecs = m.encode(texts, normalize_embeddings=True).tolist()
    return {"ok": True, "data": {"vectors": vecs, "dim": len(vecs[0]) if vecs else 0},
            "_meta": {"model": model, "count": len(texts)}}


def cosine_similarity(a: list[float], b: list[float]) -> dict:
    """Cosine similarity between two equal-length vectors. No external deps."""
    if len(a) != len(b):
        return {"ok": False, "error": f"length mismatch: {len(a)} vs {len(b)}"}
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return {"ok": True, "data": {"similarity": 0.0}}
    return {"ok": True, "data": {"similarity": dot / (na * nb)}}


# ============================================================
# OCR (PaddleOCR / Tesseract)
# ============================================================

def ocr_image(image_path: str, lang: str = "rus+eng", engine: str = "tesseract") -> dict:
    """Run OCR on an image. engine: 'tesseract' (default, local, fast) or
    'paddle' (heavier, more accurate on Cyrillic). Returns {text, _meta:{engine}}."""
    if engine == "paddle":
        try:
            from paddleocr import PaddleOCR
        except ImportError:
            return {"ok": False, "error": "paddleocr not installed",
                    "fix_hint": "pip install paddleocr", "_meta": {}}
        ocr = PaddleOCR(lang="ru" if "rus" in lang else "en")
        result = ocr.ocr(image_path, cls=True)
        text = "\n".join(line[1][0] for page in (result or []) for line in (page or []) if line)
        return {"ok": True, "data": {"text": text}, "_meta": {"engine": "paddle", "lang": lang}}
    # Default: tesseract
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return {"ok": False, "error": "pytesseract/PIL not installed",
                "fix_hint": "pip install pytesseract pillow", "_meta": {}}
    text = pytesseract.image_to_string(Image.open(image_path), lang=lang)
    return {"ok": True, "data": {"text": text}, "_meta": {"engine": "tesseract", "lang": lang}}


def ocr_pdf(pdf_path: str, lang: str = "rus+eng") -> dict:
    """OCR every page of a scanned PDF. Returns {text, pages:[per_page_text]}.

    Uses pdf2image + pytesseract. For digitally-born PDFs prefer
    `file_extract` with mime PDF (faster, no OCR needed)."""
    try:
        from pdf2image import convert_from_path
        import pytesseract
    except ImportError:
        return {"ok": False, "error": "pdf2image/pytesseract not installed",
                "fix_hint": "pip install pdf2image pytesseract", "_meta": {}}
    pages = convert_from_path(pdf_path)
    page_texts = [pytesseract.image_to_string(p, lang=lang) for p in pages]
    return {"ok": True, "data": {"text": "\n\n".join(page_texts), "pages": page_texts},
            "_meta": {"page_count": len(pages), "lang": lang}}


# ============================================================
# Schema validation (Pandera)
# ============================================================

def pandera_validate(records: list[dict], schema_json: str) -> dict:
    """Validate a list of dict-records against a Pandera DataFrameSchema
    encoded as JSON. Returns {ok, errors:[{column, row, message}]}.

    Lazy-imports pandera + pandas. schema_json example:
        {"columns": {"inn": {"dtype": "string", "nullable": false,
                              "checks": [{"type": "str_matches", "value": "^\\d{10}$|^\\d{12}$"}]}}}
    """
    try:
        import pandas as pd
        import pandera as pa
        from pandera import Column, DataFrameSchema, Check
    except ImportError:
        return {"ok": False, "error": "pandera not installed",
                "fix_hint": "pip install pandera pandas", "_meta": {}}
    spec = json.loads(schema_json) if isinstance(schema_json, str) else schema_json
    cols: dict = {}
    for name, conf in spec.get("columns", {}).items():
        checks: list = []
        for ck in conf.get("checks", []):
            t = ck.get("type")
            if t == "str_matches":
                checks.append(Check.str_matches(ck["value"]))
            elif t == "greater_than":
                checks.append(Check.greater_than(ck["value"]))
            elif t == "less_than":
                checks.append(Check.less_than(ck["value"]))
        cols[name] = Column(conf.get("dtype", "string"), nullable=conf.get("nullable", True), checks=checks)
    schema = DataFrameSchema(cols)
    df = pd.DataFrame(records)
    try:
        schema.validate(df, lazy=True)
        return {"ok": True, "data": {"valid": True, "row_count": len(df)}}
    except pa.errors.SchemaErrors as e:
        errors = []
        for _, row in e.failure_cases.iterrows():
            errors.append({
                "column": row.get("column"), "row": int(row.get("index", -1)),
                "check": row.get("check"), "value": row.get("failure_case"),
            })
        return {"ok": False, "data": {"valid": False, "errors": errors},
                "_meta": {"error_count": len(errors)}}
