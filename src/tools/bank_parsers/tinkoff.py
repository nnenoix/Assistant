"""TBank (Tinkoff) statement parser (Python port).

Port of crates/bank-parsers/src/banks/tbank.rs.

Each transaction in the extracted text spans 3+ lines:
    LINE A: 'DD.MM.YYYY'
    LINE B: 'HH:MM DD.MM.YYYY'
    LINE C: 'HH:MM [+/-]N NNN.CC ₽ [+/-]N NNN.CC ₽ [description start]'
    LINE D+ (optional): description continuation
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from .common import drop_noise, extract_inn, split_lines
from .pdf import extract_text

NAME = "Tinkoff"

TBANK_DATE_LINE_RE = re.compile(r"^\s*(\d{2})\.(\d{2})\.(\d{4})\s*$")
TIME_LINE_RE = re.compile(r"^\s*\d{2}:\d{2}\s*$")
# Rust's pdf-extract emits 'HH:MM DD.MM.YYYY' on one line; pypdf splits them
# across two lines. We accept either shape for LINE B+ recognition.
LINE_B_RE = re.compile(r"^\d{2}:\d{2}(?: \d{2}\.\d{2}\.\d{4})?$")
AMOUNT_FULL_RE = re.compile(r"([+\-]?\d[\d ]*)\.(\d{2}) ₽")
CARD_LINE_RE = re.compile(r"^\s*(\d{4})\s*$")


def _parse_date_str(s: str) -> date | None:
    m = TBANK_DATE_LINE_RE.match(s.strip())
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def _parse_amount_cents(int_part: str, frac_part: str) -> int | None:
    int_part = int_part.strip()
    sign = 1
    if int_part.startswith("-"):
        sign = -1
        int_part = int_part[1:]
    elif int_part.startswith("+"):
        int_part = int_part[1:]
    int_clean = "".join(c for c in int_part if not c.isspace())
    try:
        return sign * (int(int_clean) * 100 + int(frac_part.strip()))
    except ValueError:
        return None


def _strip_trailing_card_number(s: str) -> tuple[str, str | None]:
    """Снимает trailing «1234» (4 цифры через пробел в конце строки).
    Возвращает кортеж (cleaned_string, card_last4_or_None) — чтобы parse_text
    мог использовать извлечённую карту, а не выбрасывать её."""
    s = s.rstrip()
    if len(s) >= 5 and s[-4:].isdigit() and s[-5] == " ":
        return s[:-5].rstrip(), s[-4:]
    return s, None


def _build_description(pieces: list[str]) -> str:
    parts = []
    for p in pieces:
        t = p.strip()
        if not t:
            continue
        if CARD_LINE_RE.match(t):
            continue
        if t == "—":
            continue
        if t.startswith("Пополнения:") or t.startswith("Расходы:"):
            break
        if t.startswith("С уважением") or t.startswith("АО «ТБанк»") or t.startswith("БИК"):
            break
        if "Расходы:" in t or "Пополнения:" in t:
            break
        if all(c.isdigit() for c in t):
            continue
        parts.append(t)
    return " ".join(parts) if parts else "Неизвестно"


def _is_tx_header(lines: list[str], i: int) -> tuple[date | None, int]:
    """Return (date, index_of_amount_line) if `lines[i:]` starts a transaction.

    Two flavours:
      Rust pdf-extract:
        i:   DD.MM.YYYY          (line A)
        i+1: HH:MM DD.MM.YYYY    (line B, glued)
        i+2: HH:MM AMT ₽ AMT ₽ ... (line C)
      pypdf:
        i:   DD.MM.YYYY          (line A)
        i+1: HH:MM                (time alone)
        i+2: DD.MM.YYYY           (second date alone)
        i+3: HH:MM                (second time alone)
        i+4: -AMT ₽ -AMT ₽ desc... (no time prefix)
    """
    n = len(lines)
    d = _parse_date_str(lines[i])
    if d is None or i + 1 >= n:
        return None, -1
    next_line = lines[i + 1].strip()
    # Compact (Rust) layout
    if LINE_B_RE.match(next_line) and " " in next_line:
        if i + 2 < n and AMOUNT_FULL_RE.search(lines[i + 2].strip()):
            line_c = lines[i + 2].strip()
            if line_c[:5].count(":") == 1 and line_c[:1].isdigit():
                return d, i + 2
        return None, -1
    # Split (pypdf) layout: time, date, time, amount
    if (
        TIME_LINE_RE.match(next_line)
        and i + 2 < n
        and _parse_date_str(lines[i + 2]) is not None
        and i + 3 < n
        and TIME_LINE_RE.match(lines[i + 3].strip())
        and i + 4 < n
        and AMOUNT_FULL_RE.search(lines[i + 4].strip())
    ):
        return d, i + 4
    return None, -1


def parse_text(text: str) -> list[dict]:
    lines = split_lines(text)
    n = len(lines)
    out: list[dict] = []
    i = 0
    while i < n:
        d, amt_idx = _is_tx_header(lines, i)
        if d is None:
            i += 1
            continue
        amount_line = lines[amt_idx].strip()
        m = AMOUNT_FULL_RE.search(amount_line)
        if not m:
            i += 1
            continue
        cents = _parse_amount_cents(m.group(1), m.group(2))
        if cents is None:
            i += 1
            continue

        ruble_positions = [pos for pos, ch in enumerate(amount_line) if ch == "₽"]
        desc_start = (
            amount_line[ruble_positions[1] + len("₽"):].strip()
            if len(ruble_positions) >= 2
            else ""
        )

        i = amt_idx + 1
        desc_pieces: list[str] = []
        # Карта операции. Захватываем из любого источника:
        #   1) trailing "1234" в desc_start (после ₽)
        #   2) standalone строка "1234" в продолжении блока (CARD_LINE_RE)
        # Первая найденная — используется в tx.
        card_last4: str | None = None
        if desc_start:
            cleaned, c4 = _strip_trailing_card_number(desc_start)
            if cleaned:
                desc_pieces.append(cleaned)
            if c4 and not card_last4:
                card_last4 = c4

        while i < n:
            d_next, _ = _is_tx_header(lines, i)
            if d_next is not None:
                break
            line = lines[i].strip()
            cm = CARD_LINE_RE.match(line)
            if cm and not card_last4:
                card_last4 = cm.group(1)
            desc_pieces.append(line)
            i += 1

        description = _build_description(desc_pieces)
        tx = {
            "date": d.isoformat(),
            "amount": cents,
            "currency": "RUB",
            "kind": "Credit" if cents >= 0 else "Debit",
            "description": description,
            "raw": None,
        }
        id_key = extract_inn(description)
        if id_key:
            tx["id_key"] = id_key
        if card_last4:
            tx["card_last4"] = card_last4
        out.append(tx)
    return out


def can_parse(path: str | Path) -> bool:
    name = Path(path).name.lower()
    return (
        "справка о движении" in name
        or "о движении средств" in name
        or "движении денежных" in name
        or "tinkoff" in name
        or "tbank" in name
        or "т-банк" in name
        or "т_банк" in name
        or "движении_денежных" in name
        or "_о_движении" in name
    )


def _statement_period(txs: list[dict]) -> dict | None:
    if not txs:
        return None
    dates = [t["date"] for t in txs]
    return {"from": min(dates), "to": max(dates)}


def parse(path: str | Path) -> dict:
    text = extract_text(path)
    txs = drop_noise(parse_text(text))
    if not txs:
        raise ValueError(f"no transactions parsed from {path}")
    return {
        "bank": NAME,
        "account": None,
        "period": _statement_period(txs),
        "transactions": txs,
    }
