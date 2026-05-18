"""SberBusiness statement parser (Python port).

Port of crates/bank-parsers/src/banks/sber_business.rs. Handles four document
flavours:
    1. ПАО СБЕРБАНК (СберБизнес) — flat text with 40802… accounts
    2. ВБ Банк «Выписка из лицевого счёта» — US-format amounts (282,000.00)
    3. ВБ Банк «Выписка операций» — RU-format amounts (355 338,27)
    4. Чек по операции — single transfer, Russian-month dates
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from .common import drop_noise, extract_inn, parse_date
from .pdf import extract_text

NAME = "SberBusiness"

# ────────────────────────────────────────────────────────────────────────────
# Regexes (1:1 from Rust)
# ────────────────────────────────────────────────────────────────────────────

DATE_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4})")
RU_AMT_RE = re.compile(r"(\d[\d ]*,\d{2})")
US_AMT_RE = re.compile(r"(\d{1,3}(?:,\d{3})*\.\d{2})")
RECEIPT_AMT_RE = re.compile(r"([0-9][0-9 ]*[,.]\d{2})\s*₽")

# NB: pypdf inserts '\n' between adjacent text runs that pdf-extract (Rust)
# emits glued together. We allow optional whitespace at every boundary that
# the Rust regex treated as concatenation, so the Python parser produces
# equivalent matches on the same source PDF.
SBER_TX_START = re.compile(r"(\d{2}\.\d{2}\.\d{4})\s*(\d{20})")
VB_OP_TX_START = re.compile(r"(\d{2}\.\d{2}\.\d{4})\s*(\d{2}\.\d{2}\.\d{4})\s*(\d+)\s*БИК:")
VB_PLAT_TX_START = re.compile(
    r"(\d{2}\.\d{2}\.\d{4})\s*"
    r"(?:Платёжноепоручение|Банковскийордер|Инкассовоепоручение|Мемориальныйордер|Платежноепоручение)\s*"
    r"(\d{2}\.\d{2}\.\d{4})\s*(\d+)\s*БИК:"
)
VB_LS_TX_START = re.compile(
    r"(\d{2})\.(\d{2})\.(\d{4})\s*(\d{2})\s*(\d{1,9})\s*(\d{2})\.(\d{2})\.(\d{4})\s*(\d{20})"
)

RECEIPT_DATE_RE = re.compile(
    r"(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\s+(\d{4})"
)
OWN_ACCT_RE = re.compile(r"ЛИЦЕВОМУ СЧЕТУ\s*(\d{20})")
RS_ACCT_RE = re.compile(r"Р/с:(\d{10,20})")
WHITESPACE_RE = re.compile(r"\s+")

_RU_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────


def _ru_to_cents(s: str) -> int | None:
    cleaned = s.replace(" ", "").replace(" ", "")
    if "," not in cleaned:
        return None
    rub_str, kop_str = cleaned.split(",", 1)
    try:
        return int(rub_str) * 100 + int(kop_str)
    except ValueError:
        return None


def _us_to_cents(s: str) -> int | None:
    cleaned = s.replace(",", "")
    if "." not in cleaned:
        return None
    rub_str, kop_str = cleaned.split(".", 1)
    try:
        return int(rub_str) * 100 + int(kop_str)
    except ValueError:
        return None


def _clean_desc(s: str) -> str:
    d = WHITESPACE_RE.sub(" ", s.strip()).strip(" .,")
    return d if len(d) >= 3 else "Операция СберБизнес"


def _statement_period(txs: list[dict]) -> dict | None:
    if not txs:
        return None
    dates = [t["date"] for t in txs]
    return {"from": min(dates), "to": max(dates)}


# ────────────────────────────────────────────────────────────────────────────
# Format detectors
# ────────────────────────────────────────────────────────────────────────────


def _is_sber_pao(text: str) -> bool:
    return "ВЫПИСКА ОПЕРАЦИЙ ПО ЛИЦЕВОМУ СЧЕТУ" in text or (
        "СберБизнес." in text and "ПАО СБЕРБАНК" in text
    )


def _is_vb_bank(text: str) -> bool:
    return 'ООО "ВБ Банк"' in text


def _is_vb_ls(text: str) -> bool:
    return _is_vb_bank(text) and ("Номерстроки" in text or "Номер строки" in text)


def _is_receipt(text: str) -> bool:
    return "Чек по операции" in text or "Сумма перевода" in text


# ────────────────────────────────────────────────────────────────────────────
# ПАО СБЕРБАНК (СберБизнес)
# ────────────────────────────────────────────────────────────────────────────

_SBER_DESC_STARTERS = (
    "Плата ", "Плата за", "Оплата", "Платеж", "Перевод", "Покупка", "Отмена",
    "Комиссия", "Единый", "Страховые", "Погашение", "Выплата", "Поступление",
    "Возврат", "Зачисление", "Прием", "//Реестр", "//БЭСП", "//УФК",
)


def _extract_sber_desc(after_amounts: str) -> str:
    for starter in _SBER_DESC_STARTERS:
        pos = after_amounts.find(starter)
        if pos >= 0:
            tail = after_amounts[pos:]
            m = DATE_RE.search(tail)
            end = m.start() if m else len(tail)
            return _clean_desc(tail[:end])

    pos = after_amounts.rfind("Банк")
    if pos >= 0:
        tail = after_amounts[pos + 4 :]
        for i, ch in enumerate(tail):
            if ch.isalpha() and ch >= "Ѐ":
                desc = tail[i:]
                m = DATE_RE.search(desc)
                end = m.start() if m else len(desc)
                return _clean_desc(desc[:end])
    return ""


def _parse_sber_pao(text: str) -> list[dict]:
    own_acct_m = OWN_ACCT_RE.search(text)
    own_acct = own_acct_m.group(1) if own_acct_m else None

    starts = [m.start() for m in SBER_TX_START.finditer(text)]
    out: list[dict] = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        seg = text[start:end]

        date_m = DATE_RE.search(seg)
        if not date_m:
            continue
        d = parse_date(date_m.group(0))
        if d is None:
            continue

        cap = SBER_TX_START.search(seg)
        first_acct = cap.group(2) if cap else ""
        is_debit = (own_acct == first_acct) if own_acct else True

        ru_amounts = [m.group(0) for m in RU_AMT_RE.finditer(seg)]
        if not ru_amounts:
            continue

        cents: int | None = None
        for a in ru_amounts:
            c = _ru_to_cents(a)
            if c is not None and c > 0:
                cents = c
                break
        if cents is None:
            continue
        signed = -cents if is_debit else cents

        last_amt = None
        for m in RU_AMT_RE.finditer(seg):
            last_amt = m
        desc = _extract_sber_desc(seg[last_amt.end():]) if last_amt else ""
        if not desc:
            desc = "Операция СберБизнес"

        out.append(
            {
                "date": d.isoformat(),
                "amount": signed,
                "currency": "RUB",
                "kind": "Credit" if signed >= 0 else "Debit",
                "description": desc,
                "raw": seg,
            }
        )
    return out


# ────────────────────────────────────────────────────────────────────────────
# ВБ Банк «Выписка операций» (RU amounts)
# ────────────────────────────────────────────────────────────────────────────


def _find_desc_start_after_rs(seg: str) -> int:
    last = None
    for m in RS_ACCT_RE.finditer(seg):
        last = m
    if last is None:
        return 0
    acct_len = len(last.group(1))
    if acct_len < 10:
        return 0
    return last.start() + 4 + acct_len  # "Р/с:" + account


def _parse_vb_ru_segs(text: str, seg_starts: list[tuple[int, str]]) -> list[dict]:
    out: list[dict] = []
    for i, (start, date_str) in enumerate(seg_starts):
        end = seg_starts[i + 1][0] if i + 1 < len(seg_starts) else len(text)
        seg = text[start:end]
        d = parse_date(date_str)
        if d is None:
            continue

        post_off = _find_desc_start_after_rs(seg)
        post = seg[post_off:]

        amts: list[int] = []
        for m in list(RU_AMT_RE.finditer(post))[:4]:
            c = _ru_to_cents(m.group(0))
            if c is not None:
                amts.append(c)
        if not amts:
            continue

        debit_c, credit_c = (amts[0], amts[1]) if len(amts) >= 2 else (amts[0], 0)
        if debit_c == 0 and credit_c == 0:
            continue
        signed = credit_c - debit_c

        first_amt_m = RU_AMT_RE.search(post)
        first_amt_pos = first_amt_m.start() if first_amt_m else len(post)
        desc = _clean_desc(post[:first_amt_pos]) or "Операция СберБизнес"

        out.append(
            {
                "date": d.isoformat(),
                "amount": signed,
                "currency": "RUB",
                "kind": "Credit" if signed >= 0 else "Debit",
                "description": desc,
                "raw": seg,
            }
        )
    return out


def _parse_vb_operations(text: str) -> list[dict]:
    segs: list[tuple[int, str]] = []
    for m in VB_OP_TX_START.finditer(text):
        segs.append((m.start(), m.group(1)))
    for m in VB_PLAT_TX_START.finditer(text):
        segs.append((m.start(), m.group(1)))
    segs.sort(key=lambda x: x[0])
    # dedup by position
    out_segs: list[tuple[int, str]] = []
    seen = set()
    for s in segs:
        if s[0] in seen:
            continue
        seen.add(s[0])
        out_segs.append(s)
    return _parse_vb_ru_segs(text, out_segs)


# ────────────────────────────────────────────────────────────────────────────
# ВБ Банк «Выписка из лицевого счёта» (US amounts)
# ────────────────────────────────────────────────────────────────────────────


def _parse_vb_ls(text: str) -> list[dict]:
    starts = [m.start() for m in VB_LS_TX_START.finditer(text)]
    out: list[dict] = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        seg = text[start:end]
        m = VB_LS_TX_START.search(seg)
        if not m:
            continue
        try:
            d = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            continue

        post = seg[m.end():]

        # US amounts; exclude "XX.XX." (looks like date)
        usa_iter = []
        for am in US_AMT_RE.finditer(post):
            after = post[am.end():am.end() + 1]
            if after == ".":
                continue
            usa_iter.append(am)

        amts: list[int] = []
        for am in usa_iter[:3]:
            c = _us_to_cents(am.group(0))
            if c is not None:
                amts.append(c)
        if len(amts) < 2:
            continue
        debit_c, credit_c = amts[0], amts[1]
        if debit_c == 0 and credit_c == 0:
            continue
        signed = credit_c - debit_c

        third_m = usa_iter[2] if len(usa_iter) >= 3 else None
        desc_start = third_m.end() if third_m else 0
        desc_raw = post[desc_start:]
        cut = desc_raw.find("Итого")
        desc = _clean_desc(desc_raw[: cut if cut >= 0 else len(desc_raw)]) or "Операция СберБизнес"

        out.append(
            {
                "date": d.isoformat(),
                "amount": signed,
                "currency": "RUB",
                "kind": "Credit" if signed >= 0 else "Debit",
                "description": desc,
                "raw": seg,
            }
        )
    return out


# ────────────────────────────────────────────────────────────────────────────
# Чек по операции
# ────────────────────────────────────────────────────────────────────────────


def _parse_receipt(text: str) -> dict | None:
    if "Чек по операции" not in text and "Сумма перевода" not in text:
        return None
    m = RECEIPT_DATE_RE.search(text)
    if not m:
        return None
    try:
        day = int(m.group(1))
        month = _RU_MONTHS[m.group(2)]
        year = int(m.group(3))
        d = date(year, month, day)
    except (ValueError, KeyError):
        return None

    op_type = next(
        (
            op
            for op in (
                "Перевод по СБП",
                "Перевод клиенту СберБанка",
                "Перевод клиенту",
                "Покупка по карте",
                "Перевод",
            )
            if op in text
        ),
        "Операция",
    )
    amt_m = RECEIPT_AMT_RE.search(text)
    if not amt_m:
        return None
    amt_str = amt_m.group(1).strip()
    if "," in amt_str:
        cents = _ru_to_cents(amt_str)
    else:
        cents = _ru_to_cents(amt_str.replace(" ", "").replace(".", ","))
    if not cents:
        return None
    return {
        "date": d.isoformat(),
        "amount": -cents,
        "currency": "RUB",
        "kind": "Debit",
        "description": op_type,
        "raw": text,
    }


# ────────────────────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────────────────────


def can_parse(path: str | Path) -> bool:
    name = Path(path).name.lower()
    return (
        "сбербизнес" in name
        or "sberbusiness" in name
        or "sber_business" in name
        or ("выписка" in name and "40802" in name)
        or ("выписка" in name and "период" in name)
        or ("документ-" in name and "2026" in name)
        or "40802810" in name
    )


def parse_text(text: str) -> list[dict]:
    if _is_receipt(text) and not _is_sber_pao(text) and not _is_vb_bank(text):
        tx = _parse_receipt(text)
        if tx:
            return _enrich_id_keys([tx])
    if _is_sber_pao(text):
        return _enrich_id_keys(_parse_sber_pao(text))
    if _is_vb_bank(text):
        return _enrich_id_keys(
            _parse_vb_ls(text) if _is_vb_ls(text) else _parse_vb_operations(text)
        )
    return []


def _enrich_id_keys(txs: list[dict]) -> list[dict]:
    """Заполняем `id_key` из ИНН в описании (post-processing после парсинга).

    SberBusiness уже даёт 73.8% покрытие id_key через NER — но в B2B-выписках
    ИНН структурно зашит в `description` как «ИНН 7707083893». Структурный
    extractor работает быстрее и точнее NER для этих случаев, плюс снимает
    зависимость от Python-runtime в горячем пути.
    """
    for tx in txs:
        if "id_key" in tx and tx["id_key"]:
            continue
        id_key = extract_inn(tx.get("description", ""))
        if id_key:
            tx["id_key"] = id_key
    return txs


def parse(path: str | Path) -> dict:
    text = extract_text(path)
    txs = drop_noise(parse_text(text))
    if not txs:
        raise ValueError(f"no transactions parsed from {path}")
    own = OWN_ACCT_RE.search(text)
    return {
        "bank": NAME,
        "account": own.group(1) if own else None,
        "period": _statement_period(txs),
        "transactions": txs,
    }
