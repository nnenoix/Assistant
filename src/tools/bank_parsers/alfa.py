"""Alfa-Bank statement parser (Python port).

Port of crates/bank-parsers/src/banks/alfa.rs. Algorithm and regexes are
preserved 1:1; snapshot tests verify byte-for-byte equivalence with the Rust
output on the BankData/Альфа/ corpus.

Format (pdf-extract / pdfplumber text layer):
    Each transaction occupies 1–3 lines:
        DD.MM.YYYY CODE description ... ±N NNN,KK RU[BR]
    Long lines wrap; continuation lines do not start with a date.
    Pages are separated by header rows ("Дата проводки Код операции ...").

Algorithm:
    1. Find the "Операции по счету" marker.
    2. Group lines into blocks: a new block starts when a line begins with
       DATE+CODE; continuation lines extend the current block; the block ends
       when AMOUNT+RUR is found.
    3. Extract date, amount, description from each block.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import TypedDict

from .common import DATE_RE, drop_noise, extract_inn, parse_date, parse_money_cents
from .pdf import extract_text

NAME = "Alfa"

# ────────────────────────────────────────────────────────────────────────────
# Regexes (mirrors of the Rust ones)
# ────────────────────────────────────────────────────────────────────────────

LINE_STARTS_DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}\s")
STARTS_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{4,}\s")
# `(?<!\d)` блокирует склейку с предыдущим цифровым прибежищем (например,
# идентификатор СБП `A6055...11700` без пробела перед суммой) — без этого
# хвост ID-цифр мог захватываться в первое `\d{1,3}` суммы и раздувать её.
AMOUNT_RUR_RE = re.compile(r"(?<!\d)(\s?[+\-]?\d{1,3}(?:\s\d{3})*,\d{2})\s+RU[BR]\s*$")
OPS_MARKER_RE = re.compile(r"операции\s+по\s+счет", re.IGNORECASE)

LEADING_CODE_RE = re.compile(r"^\s*[A-Z][A-Z0-9_]{4,}\s+")
INNER_CODE_RE = re.compile(r"\b[A-Z][A-Z0-9_]{9,}\b")
NO_NDS_RE = re.compile(r"[.,]\s*Без\s+(налога\s*\(НДС\)|НДС)[.,]?\s*$", re.IGNORECASE)

UUID_PREFIX_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class _ParsedRow(TypedDict):
    date: date
    description: str
    amount_cents: int


# ────────────────────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────────────────────


def can_parse(path: str | Path) -> bool:
    name = Path(path).name.lower()
    return (
        "выписка" in name
        or "alfa" in name
        or "альфа" in name
        or bool(UUID_PREFIX_RE.match(name))
        or "tgd_" in name
        or "unsorted_" in name
    )


def parse(path: str | Path) -> dict:
    text = extract_text(path)
    transactions = parse_text(text)
    transactions = drop_noise(transactions)
    if not transactions:
        raise ValueError(f"no transactions parsed from {path}")
    period = _statement_period(transactions)
    return {
        "bank": NAME,
        "account": None,
        "period": period,
        "transactions": transactions,
    }


def parse_text(text: str) -> list[dict]:
    """Public for parity with Rust __parse_text_for_tests."""
    all_lines = text.splitlines()
    start_idx = 0
    for i, ln in enumerate(all_lines):
        if OPS_MARKER_RE.search(ln.strip()):
            start_idx = i
            break
    blocks = _group_blocks(all_lines[start_idx:])
    out: list[dict] = []
    for block in blocks:
        row = _process_block(block)
        if row is None:
            continue
        kind = "Credit" if row["amount_cents"] >= 0 else "Debit"
        # `id_key` = ИНН из описания, если найден. Структурно у Alfa ИНН
        # часто фигурирует в тексте «Оплата по договору ... ИНН XXX»;
        # для SBP-переводов ИНН отсутствует — оставляем None, NER
        # подхватит на этапе orchestrator.
        id_key = extract_inn(row["description"])
        tx = {
            "date": row["date"].isoformat(),
            "amount": row["amount_cents"],
            "currency": "RUB",
            "kind": kind,
            "description": row["description"],
            "raw": block,
        }
        if id_key:
            tx["id_key"] = id_key
        out.append(tx)
    return out


# ────────────────────────────────────────────────────────────────────────────
# Internals
# ────────────────────────────────────────────────────────────────────────────


def _is_tx_start(line: str) -> bool:
    if not LINE_STARTS_DATE_RE.match(line):
        return False
    after_date = line[10:].lstrip()
    return bool(STARTS_CODE_RE.match(after_date))


def _group_blocks(lines: list[str]) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []

    def finalize() -> None:
        if current:
            blocks.append(" ".join(current))
            current.clear()

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if _is_tx_start(line):
            finalize()
            current.append(line)
            if AMOUNT_RUR_RE.search(line):
                finalize()
        elif current:
            current.append(line)
            joined = " ".join(current)
            if AMOUNT_RUR_RE.search(joined):
                blocks.append(joined)
                current.clear()
        # lines before the first block are dropped
    finalize()
    return blocks


def _process_block(block: str) -> _ParsedRow | None:
    date_m = DATE_RE.search(block)
    if not date_m:
        return None
    d = parse_date(date_m.group(0))
    if d is None:
        return None

    amt_m = AMOUNT_RUR_RE.search(block)
    if not amt_m:
        return None
    amount_str = amt_m.group(1)
    amount_start = amt_m.start(1)

    amount_clean = "".join(c for c in amount_str if not c.isspace() or c == "")
    amount_clean = amount_clean.replace(" ", "")
    cents_raw = parse_money_cents(amount_clean)
    if cents_raw is None:
        return None

    before = block[:amount_start].rstrip()
    if before.endswith("-") or amount_str.lstrip().startswith("-"):
        cents = -abs(cents_raw)
    elif before.endswith("+") or amount_str.lstrip().startswith("+"):
        cents = abs(cents_raw)
    else:
        cents = cents_raw

    content_start = date_m.end()
    content_end = amount_start
    if content_end <= content_start:
        return None
    raw_content = block[content_start:content_end]
    description = _clean_description(raw_content)
    if len(description) < 3:
        return None
    return {"date": d, "description": description, "amount_cents": cents}


def _clean_description(raw: str) -> str:
    s = LEADING_CODE_RE.sub("", raw)
    s = INNER_CODE_RE.sub("", s)
    s = NO_NDS_RE.sub("", s)
    s = " ".join(s.split())
    return s.strip(" .,-:")


def _statement_period(transactions: list[dict]) -> dict | None:
    if not transactions:
        return None
    dates = [t["date"] for t in transactions]
    return {"from": min(dates), "to": max(dates)}
