"""UniCredit Bank statement parser (Python port).

Port of crates/bank-parsers/src/banks/unicredit.rs. Two formats:
    1. statement Prime Visa Signature Cashback (debit card, US-format amounts)
    2. ufr_stmt_physical_vypiska (current account, RU-format amounts with RUR/RUB)
"""

from __future__ import annotations

import re
from pathlib import Path

from .common import drop_noise, parse_date, split_lines
from .pdf import extract_text

NAME = "Unicredit"

DATE_LINE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}")
DATE_RE = re.compile(r"\d{2}\.\d{2}\.\d{4}")
US_AMOUNT_ANY_RE = re.compile(r"-?\d[\d,]*\.\d{2}")
RU_AMOUNT_CURRENCY_RE = re.compile(r"(-?[\d][\d\s]*,\d{2})\s*(RUR|RUB|EUR|USD)\s*$")
OP_CODE_RE = re.compile(r"^[A-Za-z0-9_]{5,}$")


def _parse_us_cents(s: str) -> int | None:
    s = s.strip()
    neg = s.startswith("-")
    s = s.lstrip("-").replace(",", "")
    if "." not in s:
        return None
    int_str, frac_str = s.split(".", 1)
    try:
        cents = int(int_str) * 100 + int(frac_str)
    except ValueError:
        return None
    return -cents if neg else cents


def _parse_ru_cents(s: str) -> int | None:
    s = s.strip()
    neg = s.startswith("-")
    s = s.lstrip("-").strip()
    cleaned = "".join(c for c in s if not c.isspace())
    if "," not in cleaned:
        return None
    int_str, frac_str = cleaned.split(",", 1)
    try:
        cents = int(int_str) * 100 + int(frac_str)
    except ValueError:
        return None
    return -cents if neg else cents


def _is_balance_line(s: str) -> bool:
    return any(
        marker in s
        for marker in (
            "Исходящий остаток", "Входящий остаток", "Opening balance",
            "Closing balance", "Поступления", "Списания", "Расходы",
            "Неподтвержденные операции", "Платежный лимит", "Текущий баланс",
        )
    )


def _clean_desc(s: str) -> str:
    s = s.removesuffix("Без НДС.").strip()
    s = " ".join(s.split())
    return s.strip(" .,")


# ────────────────────────────────────────────────────────────────────────────
# Format 1: card statement
# ────────────────────────────────────────────────────────────────────────────


def _is_card_format(text: str) -> bool:
    return (
        "Выписка по счету дебетовой карты" in text
        or "Сумма в\nвалюте счета" in text
        or "Сумма в валюте счета" in text
        or "Сумма\nоперации" in text
        or "Сумма операции" in text
    )


def _parse_card(text: str) -> list[dict]:
    out: list[dict] = []
    for raw_line in split_lines(text):
        line = raw_line.strip()
        if not DATE_LINE_RE.match(line) or _is_balance_line(line):
            continue
        amounts = list(US_AMOUNT_ANY_RE.finditer(line))
        if not amounts:
            continue
        last_m = amounts[-1]
        cents = _parse_us_cents(last_m.group(0))
        if cents is None or cents == 0:
            continue
        date_m = DATE_RE.search(line)
        if not date_m:
            continue
        d = parse_date(date_m.group(0))
        if d is None:
            continue
        dates = list(DATE_RE.finditer(line))
        desc_start = dates[1].end() if len(dates) >= 2 else dates[0].end()
        desc_end = amounts[0].start()
        if desc_start < desc_end:
            desc_raw = line[desc_start:desc_end].strip()
        else:
            desc_raw = line[: last_m.start()][date_m.end():].strip()
        for cur in ("RUB", "USD", "EUR"):
            desc_raw = desc_raw.rstrip().removesuffix(cur).rstrip()
        desc = _clean_desc(desc_raw) or "Неизвестно"
        out.append(
            {
                "date": d.isoformat(),
                "amount": cents,
                "currency": "RUB",
                "kind": "Credit" if cents >= 0 else "Debit",
                "description": desc,
                "raw": raw_line,
            }
        )
    return out


# ────────────────────────────────────────────────────────────────────────────
# Format 2: account statement
# ────────────────────────────────────────────────────────────────────────────


def _starts_new_account_record(line: str) -> bool:
    if not DATE_LINE_RE.match(line):
        return False
    after = line[10:].strip().split(None, 1)
    if not after:
        return False
    return bool(OP_CODE_RE.match(after[0]))


def _parse_account_block(lines: list[str]) -> dict | None:
    if not lines:
        return None
    first = lines[0]
    date_m = DATE_RE.search(first)
    if not date_m:
        return None
    d = parse_date(date_m.group(0))
    if d is None:
        return None

    cents = None
    currency = ""
    amount_idx = -1
    for i in range(len(lines) - 1, -1, -1):
        m = RU_AMOUNT_CURRENCY_RE.search(lines[i])
        if m:
            cents = _parse_ru_cents(m.group(1))
            currency = m.group(2)
            amount_idx = i
            break
    if cents is None or cents == 0:
        return None

    after_date = first[date_m.end():].strip()
    parts = after_date.split(None, 1)
    desc_start = parts[1].strip() if len(parts) >= 2 else ""

    desc_parts = [desc_start]
    for ln in lines[1 : amount_idx + 1]:
        m = RU_AMOUNT_CURRENCY_RE.search(ln)
        if m:
            prefix = ln[: m.start()].strip()
            if prefix:
                desc_parts.append(prefix)
        else:
            ln = ln.strip()
            if ln:
                desc_parts.append(ln)

    desc = _clean_desc(" ".join(p for p in desc_parts if p)) or "Неизвестно"
    return {
        "date": d.isoformat(),
        "amount": cents,
        "currency": currency,
        "kind": "Credit" if cents >= 0 else "Debit",
        "description": desc,
        "raw": " ".join(lines),
    }


def _parse_account(text: str) -> list[dict]:
    lines = split_lines(text)
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if _starts_new_account_record(line):
            if current:
                blocks.append(current)
                current = []
        if current or _starts_new_account_record(line):
            current.append(line)
    if current:
        blocks.append(current)

    out: list[dict] = []
    for block in blocks:
        tx = _parse_account_block(block)
        if tx:
            out.append(tx)
    return out


def parse_text(text: str) -> list[dict]:
    if _is_card_format(text):
        return _parse_card(text)
    return _parse_account(text)


def can_parse(path: str | Path) -> bool:
    name = Path(path).name.lower()
    return (
        "statement prime visa" in name
        or "ufr_stmt" in name
        or "unicredit" in name
        or "юникредит" in name
        or name.startswith("tgd_statement prime")
        or name.startswith("tgd_filecode=ufr_stmt")
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
