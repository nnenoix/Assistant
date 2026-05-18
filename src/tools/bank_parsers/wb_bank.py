"""Wildberries Банк (ООО «Вайлдберриз Банк») statement parser.

Не путать с `crates/wb` — это маркетплейс Wildberries (Ozon-like финансы).
Здесь — банк-эмитент, выдаёт стандартные «Выписка из лицевого счёта».

Format: «Выписка из лицевого счета» от ООО «Вайлдберриз Банк». Структура
табличная, **с явными колонками Дебет / Кредит** (в отличие от Модуля,
где после pdf-extract колонки схлопываются).

Заголовок таблицы:
    Номер | Дата | Вид | Номер | Дата | Счёт | Дебет | Кредит | Остаток | Назначение
    строки  проводки операции документа документа плательщика/получателя

Каждая транзакция = одна-несколько строк:
    1 04.08.2025 01 136735 04.08.2025 40702810600000000353 0.00 1,283,279.00 1,283,279.00
    Оплата  по  договору  б/н  от
    28.07.2025  за  товар.  Сумма
    1283279  RUB  без  НДС

Где первая строка содержит структурные поля (номер строки, даты, счёт,
дебет, кредит, остаток), а описание идёт следом несколькими строками.
Между транзакциями встречаются `Итого DD.MM.YYYY 0.00 X.XX` итоги дня.

Числа в US-формате (точка для копеек, запятая для тысяч): `1,283,279.00`.
Счёт плательщика 20 цифр — структурный признак, по нему можно ловить ИНН
(если он встречается в описании).
"""

from __future__ import annotations

import re
from pathlib import Path

from .common import drop_noise, extract_inn, parse_date
from .pdf import extract_text

NAME = "Wb"

# Маркер банка: «ООО \"Вайлдберриз Банк\"» либо ИНН 0102000578.
WB_HEADER_RE = re.compile(
    r"Вайлдберриз\s+Банк|ВАЙЛДБЕРРИЗ\s+БАНК|ИНН\s*0102000578",
    re.IGNORECASE,
)

# Block-start: первая строка операции.
# Формат: <row_no> <date_проводки> <op_type> <doc_no> <date_doc> <counter_acct(20)> <debit> <credit> <остаток>
# где даты в DD.MM.YYYY, числа в US-стиле `1,234.56`.
WB_BLOCK_START_RE = re.compile(
    r"^(\d+)\s+"                                  # № строки
    r"(\d{2}\.\d{2}\.\d{4})\s+"                  # Дата проводки
    r"(\d{2})\s+"                                 # Вид операции
    r"(\d+)\s+"                                   # № документа
    r"(\d{2}\.\d{2}\.\d{4})\s+"                  # Дата документа
    r"(\d{20})\s+"                                # Счёт плательщика/получателя
    r"([\d,]+\.\d{2})\s+"                        # Дебет
    r"([\d,]+\.\d{2})\s+"                        # Кредит
    r"([\d,]+\.\d{2})"                           # Остаток (после суммы)
)
# «Итого DD.MM.YYYY <debit> <credit>» — итог дня, не операция.
WB_DAILY_SUMMARY_RE = re.compile(
    r"^Итого\s+\d{2}\.\d{2}\.\d{4}\s+[\d,]+\.\d{2}\s+[\d,]+\.\d{2}",
    re.IGNORECASE,
)
WB_OWN_ACCOUNT_RE = re.compile(r"\b(\d{20})\b")


def can_parse(path: str | Path) -> bool:
    name = Path(path).name.lower()
    return (
        "wb" in name
        or "вб" in name
        or "вайлдберриз" in name
        or "wildberries" in name
    )


def parse_text(text: str) -> list[dict]:
    if not _is_wb_text(text):
        return []
    return _parse_wb(text)


def _is_wb_text(text: str) -> bool:
    return bool(WB_HEADER_RE.search(text[:3000]))


def _parse_wb(text: str) -> list[dict]:
    """Walks lines; state machine: SEEKING → IN_BLOCK_DESC.

    Block start := строка матчит WB_BLOCK_START_RE.
    Block end := следующий start или строка-итог `Итого DD.MM.YYYY ...`.
    """
    lines = [ln.rstrip() for ln in text.splitlines()]
    out: list[dict] = []

    block_date: str = ""
    block_amount: int = 0
    block_purpose_parts: list[str] = []
    in_block = False

    def emit() -> None:
        if not in_block or block_amount == 0:
            return
        purpose = " ".join(p.strip() for p in block_purpose_parts if p.strip())
        # WB склеивает текст с двойными пробелами — нормализуем.
        purpose = re.sub(r"\s+", " ", purpose).strip(" .,")
        if not purpose:
            purpose = "Операция Wildberries Банк"
        d = parse_date(block_date)
        if d is None:
            return
        tx = {
            "date": d.isoformat(),
            "amount": block_amount,
            "currency": "RUB",
            "kind": "Credit" if block_amount >= 0 else "Debit",
            "description": purpose,
            "raw": None,
        }
        # ИНН в описании — ВБ Банк часто пишет ИНН плательщика в назначении.
        id_key = extract_inn(purpose)
        if id_key:
            tx["id_key"] = id_key
        out.append(tx)

    def reset() -> None:
        nonlocal block_date, block_amount, block_purpose_parts, in_block
        block_date = ""
        block_amount = 0
        block_purpose_parts = []
        in_block = False

    for line in lines:
        s = line.strip()
        if not s:
            continue

        if WB_DAILY_SUMMARY_RE.match(s):
            # Итог дня — закрываем текущий блок если был открыт.
            emit()
            reset()
            continue

        m = WB_BLOCK_START_RE.match(s)
        if m:
            # Закрываем предыдущий блок (если был).
            emit()
            reset()
            block_date = m.group(2)
            debit_str = m.group(7)
            credit_str = m.group(8)
            block_amount = _wb_signed_cents(debit_str, credit_str)
            in_block = True
            continue

        if in_block:
            # Описание / продолжение назначения.
            block_purpose_parts.append(s)

    # Emit последний блок.
    emit()
    return out


def _wb_signed_cents(debit_str: str, credit_str: str) -> int:
    """Дебет ≠ 0 → отрицательный (списание); Кредит ≠ 0 → положительный."""
    debit = _wb_parse_money(debit_str) or 0
    credit = _wb_parse_money(credit_str) or 0
    return credit - debit


def _wb_parse_money(s: str) -> int | None:
    """`1,283,279.00` → 128327900. US-формат, запятая = тысячи."""
    t = s.strip().replace(",", "")
    if "." not in t:
        return None
    rub_str, kop_str = t.split(".", 1)
    if not rub_str.isdigit() or len(kop_str) != 2 or not kop_str.isdigit():
        return None
    return int(rub_str) * 100 + int(kop_str)


def _statement_period(txs: list[dict]) -> dict | None:
    if not txs:
        return None
    dates = [t["date"] for t in txs]
    return {"from": min(dates), "to": max(dates)}


def _extract_account(text: str) -> str | None:
    """ВБ-выписка имеет «Счет: 40802810400000006253» в шапке. Берём первое
    20-цифровое число которое начинается с 408 (расчётные счета ИП/ООО)
    или 470 (внутренние)."""
    for m in WB_OWN_ACCOUNT_RE.finditer(text[:2500]):
        digits = m.group(1)
        if digits.startswith("408"):
            return digits
    return None


def parse(path: str | Path) -> dict:
    text = extract_text(path)
    if not _is_wb_text(text):
        raise ValueError(f"не похоже на выписку ВБ Банка: {path}")
    txs = drop_noise(parse_text(text))
    if not txs:
        raise ValueError(f"no transactions parsed from {path}")
    return {
        "bank": NAME,
        "account": _extract_account(text),
        "period": _statement_period(txs),
        "transactions": txs,
    }
