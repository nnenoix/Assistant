"""Gazprombank statement parser (Python port).

Port of crates/bank-parsers/src/banks/gazprom.rs. Two document flavours:
    1. ВЫПИСКА ПО СЧЕТУ (deposit/account)
    2. ВЫПИСКА ПО КАРТЕ / ВЫПИСКА ПО СЧЕТУ КАРТЫ
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from .common import drop_noise, extract_inn, parse_money_cents
from .pdf import extract_text

NAME = "Gazprom"

DEPOSIT_TX_RE = re.compile(r"^(\d{2}\.\d{2}\.\d{4})\s+(.+?)\s+([+\-]\d[\d\s]*,\d{2})\s")
CARD_TX_START_RE = re.compile(r"^(\d{2}\.\d{2}\.\d{4})\s+\d{2}\.\d{2}\.\d{4}\s+(.*)")
CARD_AMT_FULL_RE = re.compile(r"^([+\-]\d[\d\s]*,\d{2})\s+([+\-]\d[\d\s]*,\d{2})")
CARD_AMT_SINGLE_RE = re.compile(r"^([+\-]\d[\d\s]*,\d{2})(\s+\*+\d+)?$")
SPLIT_START_RE = re.compile(r"^([+\-])(\d+)$")
SPLIT_CONT_RE = re.compile(r"^(\d+),(\d{2})\s+([+\-]\d[\d\s]*,\d{2})")
FOOTER_RE = re.compile(r"Коробов|Вице-Президент|начальник\s+Департамента", re.IGNORECASE)
DEVICE_RE = re.compile(r"Устройство:\s*(.+?)\.")


def _parse_gpb_date(s: str) -> date | None:
    s = s.strip()
    if len(s) < 10:
        return None
    try:
        return date(int(s[6:10]), int(s[3:5]), int(s[:2]))
    except ValueError:
        return None


def _detect_kind(text: str) -> str:
    prefix = text[:4000].lower()
    if "выписка по счету карты" in prefix or "выписка по карте" in prefix:
        return "card"
    return "deposit"


def _parse_deposit(text: str) -> list[dict]:
    start = text.find("Отчет по операциям")
    working = text[start:] if start >= 0 else text
    out: list[dict] = []
    for line in working.splitlines():
        line = line.strip()
        if not line or FOOTER_RE.search(line):
            continue
        m = DEPOSIT_TX_RE.match(line)
        if not m:
            continue
        d = _parse_gpb_date(m.group(1))
        if d is None:
            continue
        cents = parse_money_cents(m.group(3))
        if not cents:
            continue
        desc = m.group(2).strip() or "Операция ГПБ"
        tx = {
            "date": d.isoformat(),
            "amount": cents,
            "currency": "RUB",
            "kind": "Credit" if cents >= 0 else "Debit",
            "description": desc,
            "raw": None,
        }
        id_key = extract_inn(desc)
        if id_key:
            tx["id_key"] = id_key
        out.append(tx)
    return out


def _is_junk_line(s: str) -> bool:
    lower = s.lower()
    return (
        lower.startswith("выписка по")
        or lower.startswith("за период")
        or lower.startswith("держатель")
        or lower.startswith("дата")
        or lower.startswith("совершения")
        or lower.startswith("операции по счету")
        or lower.startswith("(списания")
        or lower.startswith("денежных средств)")
        or lower.startswith("содержание операции")
        or lower.startswith("*без учета")
        or lower.startswith("коробов")
        or lower.startswith("вице-президент")
        or s.startswith("****")
        or all(c.isascii() and c.isdigit() for c in s)
    )


def _build_card_desc(lines: list[str]) -> str:
    full = " ".join(lines)
    m = DEVICE_RE.search(full)
    if m:
        d = m.group(1).strip().replace("\n", " ").strip()
        if d:
            return d
    cleaned = full.replace("\n", " ").strip()
    if not cleaned:
        return "Операция ГПБ"
    return cleaned[:100]


def _parse_card(text: str) -> list[dict]:
    start = text.find("Отчет по операциям")
    working = text[start:] if start >= 0 else text
    lines = working.splitlines()

    out: list[dict] = []
    cur_date: date | None = None
    cur_desc: list[str] = []
    split_sign: str | None = None
    split_int: str | None = None

    def emit(net: int, d: date, desc_lines: list[str]) -> None:
        if net == 0:
            return
        desc = _build_card_desc(desc_lines)
        tx = {
            "date": d.isoformat(),
            "amount": net,
            "currency": "RUB",
            "kind": "Credit" if net >= 0 else "Debit",
            "description": desc,
            "raw": None,
        }
        id_key = extract_inn(desc)
        if id_key:
            tx["id_key"] = id_key
        out.append(tx)

    i = 0
    while i < len(lines):
        raw = lines[i].strip()
        i += 1
        if not raw or FOOTER_RE.search(raw):
            continue

        # split continuation
        if split_sign and split_int:
            m = SPLIT_CONT_RE.match(raw)
            if m:
                combined = f"{split_sign}{split_int}{m.group(1)},{m.group(2)}"
                c1 = parse_money_cents(combined) or 0
                c2 = parse_money_cents(m.group(3)) or 0
                if cur_date:
                    net = c2 if c1 == 0 else c1
                    emit(net, cur_date, cur_desc)
                cur_desc.clear()
                cur_date = None
                split_sign = None
                split_int = None
                continue
        split_sign = None
        split_int = None

        m = CARD_TX_START_RE.match(raw)
        if m:
            cur_date = _parse_gpb_date(m.group(1))
            rest = m.group(2).strip()
            cur_desc = []
            amt_m = CARD_AMT_FULL_RE.match(rest)
            if amt_m:
                c1 = parse_money_cents(amt_m.group(1)) or 0
                c2 = parse_money_cents(amt_m.group(2)) or 0
                net = c2 if c1 == 0 else c1
                desc_part = rest[: amt_m.start()].strip()
                cur_desc = [desc_part if desc_part else "Операция ГПБ"]
                if cur_date:
                    emit(net, cur_date, cur_desc)
                cur_date = None
                cur_desc = []
            elif rest:
                cur_desc.append(rest)
            continue

        m = CARD_AMT_FULL_RE.match(raw)
        if m:
            c1 = parse_money_cents(m.group(1)) or 0
            c2 = parse_money_cents(m.group(2)) or 0
            net = c2 if c1 == 0 else c1
            if cur_date:
                emit(net, cur_date, cur_desc)
            cur_date = None
            cur_desc = []
            continue

        m = CARD_AMT_SINGLE_RE.match(raw)
        if m:
            cents = parse_money_cents(m.group(1)) or 0
            if cur_date and cents != 0:
                emit(cents, cur_date, cur_desc)
                cur_date = None
                cur_desc = []
            continue

        m = SPLIT_START_RE.match(raw)
        if m:
            split_sign = m.group(1)
            split_int = m.group(2)
            continue

        if cur_date and not _is_junk_line(raw):
            cur_desc.append(raw)
    return out


def parse_text(text: str) -> list[dict]:
    if _detect_kind(text) == "card":
        return _parse_card(text)
    return _parse_deposit(text)


def can_parse(path: str | Path) -> bool:
    name = Path(path).name.lower()
    return (
        "карте_7105" in name
        or "вкладу_2645" in name
        or "счету_вкладу" in name
        or "gpb" in name
        or "gazprom" in name
        or "газпром" in name
        or ("выписка" in name and ("7105" in name or "2645" in name))
    )


def _statement_period(txs: list[dict]) -> dict | None:
    if not txs:
        return None
    dates = [t["date"] for t in txs]
    return {"from": min(dates), "to": max(dates)}


def parse(path: str | Path) -> dict:
    text = extract_text(path)
    txs = drop_noise(parse_text(text))
    return {
        "bank": NAME,
        "account": None,
        "period": _statement_period(txs),
        "transactions": txs,
    }
