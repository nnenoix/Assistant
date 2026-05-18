"""Tochka (АО «Точка») statement parser (Python).

В корпусе `BankData/Точка/` пока только один PDF — он оказался **счётом
на оплату**, не выпиской (statement_filter его отбрасывает до парсера).
Поэтому реальный формат **выписки** Точки нам пока неизвестен.

Этот парсер написан как «расширяемый стаб»:

* `can_parse` — определяет файл по filename markers (`tochka`, `точка`,
  `tochka_bank`).
* `parse_text` — возвращает `[]` (нет транзакций), но **не падает** —
  combo получит чистый «empty statement», вместо `NotImplemented` ошибки.
* Когда у нас появится реальная выписка Точки (а её формат, скорее всего,
  ближе всего к Газпрому — block-style с `Дата` / `Контрагент` / `Сумма`
  на отдельных строках), достаточно дописать `_parse_blocks` и
  переключить `parse_text` на него.

NB: Tochka в `crates/bank-parsers/src/banks/tochka.rs` остаётся stub'ом
(`NotImplemented`). Маршрутизация на этот python-парсер происходит через
`PythonBankParser` если задан `COMBO_USE_PYTHON_PARSERS=Tochka`.
"""

from __future__ import annotations

import re
from pathlib import Path

from .common import drop_noise, parse_date, parse_money_cents
from .pdf import extract_text

NAME = "Tochka"

# Маркер банка в шапке: «Точка», «АО Точка», «tochka», «БИК 044525104»
# (БИК Точка-банка — стабильный identifier).
TOCHKA_HEADER_RE = re.compile(
    r"АО\s+«?Точка|Точка\s+банк|tochka\.?ru|БИК\s+044525104",
    re.IGNORECASE,
)


def can_parse(path: str | Path) -> bool:
    name = Path(path).name.lower()
    return "tochka" in name or "точка" in name


def parse_text(text: str) -> list[dict]:
    """Парсит текст PDF-выписки Точки.

    Когда у нас будет реальная выборка выписок Точки, здесь появится
    block-style парсер. Пока возвращает пустой список — чтобы парсер
    не падал на «не-выписочных» Точка-PDF (Счёт/Акт/КУДиР), которые
    statement_filter уже отрезает на уровне выше.
    """
    if not _is_tochka_text(text):
        return []
    # Future: implement once we have real statement samples.
    # See module docstring for expected format.
    return _parse_block_format(text)


def _is_tochka_text(text: str) -> bool:
    head = text[:3000]
    return bool(TOCHKA_HEADER_RE.search(head))


# ── Block-style parser (наброcок, активируется когда добавим тесты) ────────

# Граница транзакции — строка `<DD.MM.YYYY>` в начале логической строки.
_TX_DATE_RE = re.compile(r"^(\d{2}\.\d{2}\.\d{4})\s*$|^(\d{2}\.\d{2}\.\d{4})\s+")
# Сумма с валютой: «1 234,56 ₽» / «-1 234,56 RUB»
_AMOUNT_RE = re.compile(
    r"([+\-−]?[\d\s\xa0]+,\d{2})\s*(?:₽|RUB|RUR)?",
)
# ИНН в описании
_INN_RE = re.compile(r"\bИНН\s*:?\s*(\d{10}|\d{12})\b")


def _parse_block_format(text: str) -> list[dict]:
    """Block-style парсер.

    Каждая транзакция — multi-line блок:
        02.01.2026
        Перевод физ. лицу СБП
        Получатель: Иванов И. И.
        ИНН 123456789012
        +5 000,00 ₽

    Пока без реальных тестовых данных. Активируется когда в `BankData/Точка/`
    появится настоящая выписка.
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    out: list[dict] = []
    block: list[str] = []

    def flush():
        if not block:
            return
        joined = " ".join(block)
        # Нормализуем Unicode minus (`−`, U+2212) → ASCII `-`. Точка
        # любит «типографически правильный» минус в PDF — наш
        # `parse_money_cents` понимает только ASCII.
        joined_norm = joined.replace("−", "-").replace("\xa0", " ")
        date_match = re.search(r"\b(\d{2}\.\d{2}\.\d{4})\b", joined_norm)
        if not date_match:
            return
        d = parse_date(date_match.group(1))
        if d is None:
            return
        amt_match = _AMOUNT_RE.search(joined_norm)
        if not amt_match:
            return
        amount_token = amt_match.group(1).strip()
        cents = parse_money_cents(amount_token)
        if cents is None or cents == 0:
            return
        # Знак уже в `cents` (parse_money_cents съел `+`/`-`). Если он
        # отрицательный — Debit.
        kind = "Credit" if cents > 0 else "Debit"
        description = re.sub(r"\s+", " ", joined).strip()[:500]
        inn_m = _INN_RE.search(joined)
        tx = {
            "date": d.isoformat(),
            "amount": cents,
            "currency": "RUB",
            "kind": kind,
            "description": description or "Операция Точка",
            "raw": joined,
        }
        if inn_m:
            tx["id_key"] = inn_m.group(1)
        out.append(tx)

    for ln in lines:
        s = ln.strip()
        if _TX_DATE_RE.match(s):
            flush()
            block = [s]
        elif block:
            block.append(s)
    flush()
    return out


def parse(path: str | Path) -> dict:
    text = extract_text(path)
    txs = drop_noise(parse_text(text))
    period = _statement_period(txs)
    return {
        "bank": NAME,
        "account": _extract_account(text),
        "period": period,
        "transactions": txs,
    }


def _statement_period(txs: list[dict]) -> dict | None:
    if not txs:
        return None
    dates = [t["date"] for t in txs]
    return {"from": min(dates), "to": max(dates)}


def _extract_account(text: str) -> str | None:
    m = re.search(r"(?:Расч[её]тный\s+счёт|сч\.?\s*№)\s*[:№]?\s*(\d{20})", text)
    return m.group(1) if m else None
