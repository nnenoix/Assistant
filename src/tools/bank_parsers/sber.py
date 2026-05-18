"""Sber (consumer card / deposit) statement parser (Python port).

Port of crates/bank-parsers/src/banks/sber.rs.

Поддерживает два формата:

1. **Карточный** («Выписка по дебетовой карте», «История операций»). Для
   него работает `_parse_card_text` — старый алгоритм: блок начинается с
   `DD.MM.YYYY ... <сумма> ...`.

2. **Депозитный** («Выписка по вкладу», «Выписка по счёту "Накопительный
   счёт"»). Совершенно другая раскладка: после маркера «Расшифровка
   операций» идут блоки вида:

       29.08.2025 Зачисление
       к/с 40817 810 5 3804 9277106
       02, № 6758444202-13
       Плательщик: КРЕСТИНИНА ЛАНА
       ...
       +100 000,00 658 917,96

   То есть первая строка `<DATE> <ОперацияТип>`, далее много строк
   реквизитов, и в конце `<sign-amount> <balance>`. Иногда сумма+баланс
   уезжают на ту же строку, что и шифр документа:
       -05, № 6812551723-8 -10 555,71 550 000,00

Если в тексте есть маркер «движений денежных средств по счёту не
производилось» — выписка пустая, возвращаем 0 транзакций (это валидный
кейс: депозит пролежал период без движения).
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from .common import (
    DATE_RE,
    HEAD_JUNK_RE,
    MONEY_RE,
    drop_noise,
    extract_card_last4 as _extract_card_last4,
    extract_inn,
    parse_date,
    parse_money_cents,
    split_lines,
)
from .pdf import extract_text

NAME = "Sber"

# Маркеры депозитного формата. Достаточно одного из них в первых ~2000
# символах текста, чтобы переключиться на `_parse_deposit_text`.
DEPOSIT_HEADERS = (
    "Выписка по вкладу",
    "Выписка по счёту «Накопительный счёт",
    "Выписка по счёту \"Накопительный счёт",
    "Выписка по счету «Накопительный счёт",
)
DEPOSIT_OPERATIONS_MARKER = "Расшифровка операций"
EMPTY_DEPOSIT_MARKER = "движений денежных средств по счёту не производилось"

TRIGGER_KEEP_DETAILS = (
    "Перевод с карты",
    "Перевод на карту",
    "Перевод СБП",
    "Оплата по QR-коду СБП",
    "Оплата по QR–коду СБП",
    "Прочие расходы",
    "Прочие операции",
    "Прочие выплаты",
    "Пополнение",
)

TRIGGER_USE_CATEGORY = (
    "Рестораны и кафе",
    "Автомобиль",
    "Супермаркеты",
    "Здоровье и красота",
    "Транспорт",
    "Отдых и развлечения",
    "Выдача наличных",
    "Все для дома",
    "Коммунальные платежи",
    "Одежда и аксессуары",
    "Связь, телеком",
    "Фастфуд",
)

_KEEP_LC = tuple(s.lower() for s in TRIGGER_KEEP_DETAILS)
_CATEGORY_LC = tuple(s.lower() for s in TRIGGER_USE_CATEGORY)

OPERATION_SPLIT_RE = re.compile(r"\.?\s*[ОO]перация\s", re.IGNORECASE)
LEADING_DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}\s*")
LINE_STARTS_WITH_DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}")

def _group_blocks(lines: list[str]) -> list[str]:
    blocks: list[str] = []
    current_parts: list[str] = []
    started = False
    for line in lines:
        starts_date = bool(LINE_STARTS_WITH_DATE_RE.match(line))
        has_money = bool(MONEY_RE.search(line))
        is_new = starts_date and has_money
        if is_new:
            started = True
            if current_parts:
                blocks.append(" ".join(current_parts))
                current_parts = []
            current_parts.append(line)
        elif started:
            current_parts.append(line)
    if current_parts:
        blocks.append(" ".join(current_parts))
    return blocks


def _process_block(text: str, default_card: str | None = None) -> dict | None:
    dates = list(DATE_RE.finditer(text))
    moneys = list(MONEY_RE.finditer(text))
    if not dates or not moneys:
        return None
    first_date_m = dates[0]
    d = parse_date(first_date_m.group(0))
    if d is None:
        return None

    plus_idx = None
    for i, m in enumerate(moneys):
        if m.group(0).lstrip().startswith("+"):
            plus_idx = i
            break
    if plus_idx is not None:
        amount_m, is_income = moneys[plus_idx], True
    else:
        idx = len(moneys) - 2 if len(moneys) >= 2 else 0
        amount_m, is_income = moneys[idx], False

    cents = parse_money_cents(amount_m.group(0))
    if cents is None:
        return None
    cents = abs(cents)
    if not is_income:
        cents = -cents

    desc1_raw = text[first_date_m.end() : amount_m.start()]
    desc1 = HEAD_JUNK_RE.sub("", desc1_raw).strip()

    last_money_end = moneys[-1].end()
    raw_tail = LEADING_DATE_RE.sub("", text[last_money_end:].strip())
    desc2 = OPERATION_SPLIT_RE.split(raw_tail, maxsplit=1)[0].strip().strip(" .,")

    desc1_lc = desc1.lower()
    is_transfer = any(t in desc1_lc for t in _KEEP_LC)

    final = desc1
    if is_transfer:
        if desc2:
            final = desc2
    else:
        is_cat = False
        for cat_lc, cat_orig in zip(_CATEGORY_LC, TRIGGER_USE_CATEGORY, strict=True):
            if desc1_lc.startswith(cat_lc):
                final = cat_orig
                is_cat = True
                break
        if not is_cat and desc2:
            final = desc2
    if not final:
        final = "Неизвестно"

    tx = {
        "date": d.isoformat(),
        "amount": cents,
        "currency": "RUB",
        "kind": "Credit" if cents >= 0 else "Debit",
        "description": final,
        "raw": text,
    }
    # У Sber-card в описании «Перевод от/на ФИО» ИНН отсутствует,
    # но иногда в `desc1` (полный текст до суммы) есть фраза
    # «по договору с ИНН XXX» — попробуем извлечь.
    id_key = extract_inn(text)
    if id_key:
        tx["id_key"] = id_key
    # Карта операции: сначала смотрим внутри самого блока (вдруг в выписке
    # есть несколько карт, привязанных к счёту, и каждая операция помечена
    # своей), иначе — fallback на основную карту выписки.
    block_card = _extract_card_last4(text, scan_chars=len(text))
    card = block_card or default_card
    if card:
        tx["card_last4"] = card
    return tx


def parse_text(text: str) -> list[dict]:
    """Public entry: маршрутизирует на карточный или депозитный парсер."""
    if _is_deposit_format(text):
        # Депозит: `Открытие вклада` / `Закрытие вклада` — реальные операции,
        # «входящий остаток» появляется внутри формулы капитализации в
        # строке-описании. Отключаем balance/lifecycle группы фильтра,
        # оставляем только безусловные summary-сводки.
        return drop_noise(_parse_deposit_text(text), balance=False, lifecycle=False)
    # Основная карта выписки (из шапки) — fallback для всех операций где
    # внутри блока номер карты не указан явно.
    default_card = _extract_card_last4(text)
    blocks = _group_blocks(split_lines(text))
    out: list[dict] = []
    for b in blocks:
        row = _process_block(b, default_card=default_card)
        if row:
            out.append(row)
    return drop_noise(out)


# ── Depository format ─────────────────────────────────────────────────────

# Линия-старт блока: «29.08.2025 Зачисление» (дата + название операции).
DEPOSIT_BLOCK_START_RE = re.compile(r"^(\d{2}\.\d{2}\.\d{4})\s+(\S.+)$")

# Сумма+баланс в одной строке, либо как самостоятельная строка, либо в
# хвосте предыдущей. NBSP (\xa0) встречается у Sber между разрядами.
DEPOSIT_AMT_BAL_RE = re.compile(
    r"(?P<prefix>.*?)\s*"
    r"(?P<amount>[+\-]\d[\d \xa0]*,\d{2})\s+"
    r"(?P<balance>\d[\d \xa0]*,\d{2})\s*$"
)


def _is_deposit_format(text: str) -> bool:
    head = text[:2500]
    return any(h in head for h in DEPOSIT_HEADERS)


def _parse_deposit_text(text: str) -> list[dict]:
    if EMPTY_DEPOSIT_MARKER in text:
        # Депозит без движений за период — нормальный кейс, возвращаем
        # пустой список (StatementRepo всё равно сохранит запись с tx=0).
        return []

    lines = split_lines(text)
    # Найти точку входа в раздел «Расшифровка операций».
    start = 0
    for i, ln in enumerate(lines):
        if DEPOSIT_OPERATIONS_MARKER in ln:
            start = i + 1
            break
    work = lines[start:]

    out: list[dict] = []
    block_date: date | None = None
    block_op: str = ""
    block_lines: list[str] = []

    def emit(prefix_text: str, amount_str: str) -> None:
        if block_date is None:
            return
        cents = parse_money_cents(amount_str)
        if cents is None:
            return
        # Описание = тип операции + накопленные строки + хвост перед суммой.
        desc_parts = [block_op] + block_lines
        if prefix_text:
            desc_parts.append(prefix_text)
        description = _normalize_deposit_description(" ".join(desc_parts))
        out.append({
            "date": block_date.isoformat(),
            "amount": cents,
            "currency": "RUB",
            "kind": "Credit" if cents >= 0 else "Debit",
            "description": description,
            "raw": " | ".join([f"{block_date.isoformat()} {block_op}"] + block_lines + [f"{amount_str}"]),
        })

    def reset_block() -> None:
        nonlocal block_date, block_op, block_lines
        block_date = None
        block_op = ""
        block_lines = []

    for raw in work:
        line = raw.strip()
        if not line:
            continue

        start_m = DEPOSIT_BLOCK_START_RE.match(line)
        if start_m:
            # Открываем новый блок. Если предыдущий не закрыт — теряем
            # его (битый PDF). На реальных файлах не встречается.
            d = parse_date(start_m.group(1))
            if d is None:
                continue
            block_date = d
            block_op = start_m.group(2).strip()
            block_lines = []
            continue

        if block_date is None:
            # Не в блоке — игнорим (служебные строки, повтор хедера на
            # 2-й странице и т.п.).
            continue

        amt_m = DEPOSIT_AMT_BAL_RE.match(line)
        if amt_m:
            # Закрытие блока: сумма+баланс на отдельной строке (или
            # в хвосте после реквизитов).
            prefix = (amt_m.group("prefix") or "").strip()
            emit(prefix, amt_m.group("amount"))
            reset_block()
            continue

        block_lines.append(line)

    return out


_DESC_NOISE_RE = re.compile(
    r"(?:к/с\s+\d[\d \xa0]+|№\s*[\w\-]+|^\d{2},?\s*$)",
    re.IGNORECASE,
)
# Формула капитализации вклада: «За период с … = X,YY ₽». Пробивает фильтр
# `drop_noise` через слово «остаток» и не несёт пользы для ABC. Срезаем.
_CAPITALIZATION_FORMULA_RE = re.compile(
    r"\s*За период с .*?(?:=\s*[\d \xa0,\.]+\s*₽|$)",
    re.IGNORECASE | re.DOTALL,
)
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_deposit_description(s: str) -> str:
    """Снимаем «к/с …», номера документов, формулу капитализации вклада,
    лишние пробелы. На выходе — читаемое назначение, чтобы NER ловил
    ИНН/ORG/PER, а `drop_noise` не зацеплялся за «остаток» из формулы."""
    s = _CAPITALIZATION_FORMULA_RE.sub(" ", s)
    s = _DESC_NOISE_RE.sub(" ", s)
    s = _WHITESPACE_RE.sub(" ", s).strip(" .,-:")
    return s or "Неизвестно"


def can_parse(path: str | Path) -> bool:
    name = Path(path).name.lower()
    if name.startswith("sber") or name.startswith("сбер"):
        return True
    return "выписка" in name and ("дебетов" in name or "сбер" in name)


def _statement_period(txs: list[dict]) -> dict | None:
    if not txs:
        return None
    dates = [t["date"] for t in txs]
    return {"from": min(dates), "to": max(dates)}


def parse(path: str | Path) -> dict:
    text = extract_text(path)
    txs = parse_text(text)
    if not txs:
        # Депозитная выписка может законно быть пустой («движений ... не
        # производилось») — это валидный документ, не ошибка парсера.
        # В этом случае сохраняем Statement с tx=[], orchestrator примет.
        if _is_deposit_format(text) and EMPTY_DEPOSIT_MARKER in text:
            return {
                "bank": NAME,
                "account": _extract_deposit_account(text),
                "period": _extract_deposit_period(text),
                "transactions": [],
            }
        raise ValueError(f"no transactions parsed from {path}")
    return {
        "bank": NAME,
        "account": _extract_deposit_account(text) if _is_deposit_format(text) else None,
        "period": _extract_deposit_period(text) or _statement_period(txs),
        "transactions": txs,
    }


# ── Account / period extractors for deposit format ───────────────────────

_DEPOSIT_ACCOUNT_RE = re.compile(r"Номер счёта\s+(\d[\d \xa0]+)")
_DEPOSIT_PERIOD_RE = re.compile(
    r"ИТОГО\s+ПО\s+ОПЕРАЦИЯМ\s+ЗА\s+ПЕРИОД\s+"
    r"(\d{2}\.\d{2}\.\d{4})\s*[—\-]\s*(\d{2}\.\d{2}\.\d{4})"
)


def _extract_deposit_account(text: str) -> str | None:
    m = _DEPOSIT_ACCOUNT_RE.search(text)
    if not m:
        return None
    cleaned = re.sub(r"[\s\xa0]+", "", m.group(1))
    return cleaned if len(cleaned) == 20 and cleaned.isdigit() else None


def _extract_deposit_period(text: str) -> dict | None:
    m = _DEPOSIT_PERIOD_RE.search(text)
    if not m:
        return None
    d_from = parse_date(m.group(1))
    d_to = parse_date(m.group(2))
    if d_from is None or d_to is None:
        return None
    return {"from": d_from.isoformat(), "to": d_to.isoformat()}
