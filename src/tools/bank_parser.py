"""Russian bank statement parser tool.

Wraps src/tools/bank_parsers/ (production-grade parser ported from D:\\combo,
Rust-mirror code, snapshot-tested against real bank corpora):
  - 13 banks: Сбер (физ + бизнес), Альфа, Т-Банк (Тинькофф), Газпромбанк, ВТБ,
    Райффайзен, ЮниКредит, Ozon, Modulbank, Точка, Wildberries
  - 1С client-bank .txt export (ClientBank1C format)

Three-tier PDF text extraction (pypdf → pdfplumber → pdftotext) handles
encrypted, exotic, and layout-resistant statements.
"""
from __future__ import annotations

from pathlib import Path

from src.tools.bank_parsers import dispatch as _dispatch


SUPPORTED_BANKS = [
    "alfa", "sber", "sber_business", "tinkoff", "gazprom", "vtb",
    "raif", "unicredit", "ozon", "modul", "tochka", "wb_bank",
    "clientbank_1c",
]


def parse_bank_statement(file_path: str, bank_hint: str | None = None) -> dict:
    """Parse a bank-statement PDF (or 1С .txt). Auto-detects bank by content
    signatures; pass `bank_hint` to skip detection (e.g. when the user said
    "это Сбер бизнес").

    Returns the Statement dict:
        {bank, account_last4?, transactions: [{date, description, amount_cents,
        inn?, counterparty?, ...}], ...}

    Raises ValueError if no parser matched (file isn't a recognized format).
    Raises FileNotFoundError if the path doesn't exist.

    Sample sizes: amount is in KOPECKS (not rubles) — multiply by 0.01 for ₽.
    """
    p = Path(file_path).resolve()
    if not p.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    return _dispatch.parse_statement(str(p), bank_hint=bank_hint)


def detect_bank(file_path: str) -> dict:
    """Quick detection without full parse — returns {bank} or {bank: None,
    error: 'no parser matched'}. Useful to confirm a file is a recognizable
    bank statement before committing to a multi-second parse.
    """
    p = Path(file_path).resolve()
    if not p.exists():
        return {"error": f"File not found: {file_path}"}

    for parser in _dispatch._registry():
        try:
            if parser.can_parse(p):
                return {"bank": parser.NAME}
        except Exception:
            continue
    return {"bank": None, "error": "no parser matched"}


def list_supported_banks() -> dict:
    """List bank names the parser knows how to handle."""
    return {"banks": SUPPORTED_BANKS, "count": len(SUPPORTED_BANKS)}
