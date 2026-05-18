"""OzonBank statement parser (Python port).

Port of crates/bank-parsers/src/banks/ozon.rs. Two flavours:
    1. New: «Справка о движении средств» (individual; amounts '- 238.00 ₽')
    2. Old: «Выписка по счету» (RKO; amounts '33265,51', sign unknown → all Debit)
"""

from __future__ import annotations

import re
from datetime import date as _date
from pathlib import Path

from .common import DATE_RE, drop_noise, parse_date, split_lines
from .pdf import extract_text

NAME = "Ozon"

LINE_DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}")
AMOUNT_RUB_RE = re.compile(r"([+\-])\s*([\d][\d\s]*)\.\s*(\d{2})\s*₽")
AMOUNT_OLD_RE = re.compile(r"^([\d][\d\s]*),(\d{2})(?:\s|$)")
LEADING_TIME_RE = re.compile(r"^\d{2}:\d{2}:\d{2}\s*")
LEADING_DOCNUM_RE = re.compile(r"^\d{8,12}\s+")
WHITESPACE_RE = re.compile(r"\s+")
NEW_FORMAT_RE = re.compile(r"Справка\s+о\s+движении\s+средств", re.IGNORECASE)
JUNK_LINE_RE = re.compile(
    r"""(?ix)
    ^\d{2}:\d{2}:\d{2}$
    | ^\d{2}:\d{2}$
    | ^[0-9]{6,12}$
    | ^ИНН[:/]?\s*\d+
    | ^Р/С:
    | ^БИК:
    | ^Дата\s+операции
    | ^Документ$
    | ^Назначение\s+платежа
    | ^Сумма\s+операции
    | ^Российские\s+рубли
    | ^Валюта$
    | ^Входящий\s+остаток
    | ^Исходящий\s+остаток
    | ^Итого\s+(зачислений|списаний)
    | ^С\s+уважением
    | ^Руководитель
    | ^мидл-офисных
    | ^С\.А\.
    | ^Справка\s+сформирована
    | ^Всего\s+документов
    | ^Итого\s+обороты
    | ^Поступления
    | ^Расходы
    | ^Номер\s+документа
    | ^Дебет$
    | ^Кредит$
    | ^Контрагент
    | ^Наименование
    | ^Cчёт,\s+БИК
    | ^Дата$
    """
)


def _is_new_format(text: str) -> bool:
    return bool(NEW_FORMAT_RE.search(text))


def _parse_new_amount(line: str) -> int | None:
    m = AMOUNT_RUB_RE.search(line)
    if not m:
        return None
    sign = 1 if m.group(1).strip() == "+" else -1
    int_part = "".join(c for c in m.group(2) if not c.isspace())
    try:
        return sign * (int(int_part) * 100 + int(m.group(3)))
    except ValueError:
        return None


def _parse_old_amount(line: str) -> int | None:
    m = AMOUNT_OLD_RE.match(line)
    if not m:
        return None
    int_part = "".join(c for c in m.group(1) if not c.isspace())
    try:
        return int(int_part) * 100 + int(m.group(2))
    except ValueError:
        return None


def _parse_block_date(line: str) -> _date | None:
    m = DATE_RE.search(line)
    if not m:
        return None
    try:
        return _date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def _group_blocks_new(lines: list[str]) -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] = []
    started = False
    for line in lines:
        if LINE_DATE_RE.match(line):
            if started and current:
                blocks.append(current)
                current = []
            started = True
            current.append(line)
        elif started:
            current.append(line)
    if current:
        blocks.append(current)
    return blocks


def _parse_block_new(block: list[str]) -> dict | None:
    if not block:
        return None
    d = _parse_block_date(block[0])
    if d is None:
        return None
    cents = None
    amount_idx = 0
    for i, line in enumerate(block):
        if "₽" in line:
            c = _parse_new_amount(line)
            if c is not None:
                cents = c
                amount_idx = i
                break
    if cents is None:
        return None
    desc_parts: list[str] = []
    desc_slice = block[1:amount_idx] if amount_idx > 1 else []
    for line in desc_slice:
        t = line.strip()
        if not t or JUNK_LINE_RE.match(t):
            continue
        if all(c.isdigit() or c == "-" for c in t):
            continue
        cleaned = LEADING_TIME_RE.sub("", t).strip()
        if cleaned:
            cleaned = LEADING_DOCNUM_RE.sub("", cleaned)
            if cleaned:
                desc_parts.append(cleaned)
    if desc_parts:
        description = WHITESPACE_RE.sub(" ", " ".join(desc_parts)).strip()
    else:
        description = "Неизвестно"
    return {
        "date": d.isoformat(),
        "amount": cents,
        "currency": "RUB",
        "kind": "Credit" if cents >= 0 else "Debit",
        "description": description,
        "raw": "\n".join(block),
    }


def _parse_new_format(text: str) -> list[dict]:
    blocks = _group_blocks_new(split_lines(text))
    out = []
    for b in blocks:
        tx = _parse_block_new(b)
        if tx and tx["amount"] != 0:
            out.append(tx)
    return out


def _try_parse_old_block(block: list[str], d: _date) -> dict | None:
    if not block:
        return None
    cents = None
    desc_lines: list[str] = []
    for line in block:
        if cents is None:
            c = _parse_old_amount(line)
            if c is not None:
                cents = c
                after = AMOUNT_OLD_RE.sub("", line).strip()
                if after and not all(ch.isdigit() or ch == "/" for ch in after):
                    desc_lines.append(after)
                continue
        t = line.strip()
        if t and not JUNK_LINE_RE.match(t):
            desc_lines.append(t)
    if cents is None or cents == 0:
        return None
    description = WHITESPACE_RE.sub(" ", " ".join(desc_lines)).strip() or "Неизвестно"
    return {
        "date": d.isoformat(),
        "amount": -cents,
        "currency": "RUB",
        "kind": "Debit",
        "description": description,
        "raw": "\n".join(block),
    }


def _parse_old_format(text: str) -> list[dict]:
    lines = split_lines(text)
    out: list[dict] = []
    current_date: _date | None = None
    current_block: list[str] = []
    in_data = False
    for line in lines:
        if "Назначение платежа" in line:
            in_data = True
            continue
        if "Всего документов" in line or "Итого обороты" in line:
            if current_date:
                tx = _try_parse_old_block(current_block, current_date)
                if tx:
                    out.append(tx)
            break
        if not in_data:
            continue
        if LINE_DATE_RE.match(line):
            if current_date:
                tx = _try_parse_old_block(current_block, current_date)
                if tx:
                    out.append(tx)
            new_d = parse_date(line)
            if new_d is not None:
                current_date = new_d
                current_block = [line]
        elif current_date is not None:
            current_block.append(line)
    if current_date:
        tx = _try_parse_old_block(current_block, current_date)
        if tx:
            out.append(tx)
    return out


def parse_text(text: str) -> list[dict]:
    return _parse_new_format(text) if _is_new_format(text) else _parse_old_format(text)


def can_parse(path: str | Path) -> bool:
    name = Path(path).name.lower()
    if "ozonbank" in name or "ozon_bank" in name or "о_движении" in name or "receipt" in name:
        return True
    if (name.startswith("tgd_") or "_tgd_" in name) and ("%d0%be" in name or "ozon" in name):
        return True
    return False


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
