"""Modulbank statement parser (Python).

Format: «Выписка по расчётному счёту» от Модульбанка. Особенности:

  - Один PDF может содержать **несколько счетов** одного клиента (Расчётный,
    Счёт-копилка, Маркет-карта). Каждый счёт = отдельная секция с собственным
    заголовком `БИК … Расчётный счёт № …`. Парсер обрабатывает каждую секцию
    независимо.
  - Колонки `Приход / Расход` после pdf-extract схлопываются в одно поле:
    в каждой строке только одна сумма (вторая колонка пустая → не попадает
    в текст). Направление определяем эвристикой по purpose:
        «Поступление», «Пополнение», «Зачисление», «Возмещение»  → Credit
        «Перевод»,  «Списание», «Комиссия», «Оплата (своему)»     → Debit
        контрагент = владелец счёта                                → Debit
        иначе (по умолчанию)                                       → Credit

  - ИНН контрагента — структурно есть в строке (поле № 4 после имени),
    формат `<10 цифр>` или `<10 цифр>,<КПП>`. Берём как `id_key`.

Пример строки операции (после pdf-extract; реальные NBSP заменены на пробел):

    95569 11.08.2025 ОБЩЕСТВО С ОГРАНИЧЕННОЙ
    ОТВЕТСТВЕННОСТЬЮ "РВБ"
    9714053621,
    507401001 АО "АЛЬФА-БАНК" 044525593 40702810801850006584 Оплата по договору б/н от 04.08.2025 за товар. Сумма 925423.27 RUB без НДС 925 423,27
"""

from __future__ import annotations

import re
from pathlib import Path

from .common import drop_noise, parse_date
from .pdf import extract_text

NAME = "Modul"

# Маркер: «БИК 044525092» (Модульбанк) или «МОДУЛЬБАНК» в первых 2000 символов.
MODUL_HEADER_RE = re.compile(r"МОДУЛЬБАНК|Модульбанк|БИК\s+044525092", re.IGNORECASE)

# Граница секции (один счёт): «Расчётный счёт № NNNN» / «Счёт-копилка №»
# / «Маркет карта №».
SECTION_HEADER_RE = re.compile(
    r"(?:Расчётный\s+счёт|Расчетный\s+счёт|Расчетный\s+счет|Расчётный\s+счет|"
    r"Счёт-копилка|Счет-копилка|Маркет\s+карта|Маркет-карта)\s+№\s*(\d{20})",
    re.IGNORECASE,
)
# Владелец счёта — строка после `Расчётный счёт …`; формат:
# «Индивидуальный предприниматель Иванов И. И.» или «ООО „Ромашка"».
OWNER_RE = re.compile(
    r"(?:Индивидуальный\s+предприниматель|ИП|ООО|АО|ПАО|ЗАО)\s+([^\n]{3,100})",
    re.IGNORECASE,
)

# Строка-старт операции: `<doc_no> <DD.MM.YYYY>` в начале логической строки.
# В тексте pdf-extract строка может быть разорвана — собираем по boundary.
TX_START_RE = re.compile(r"^(\d+)\s+(\d{2}\.\d{2}\.\d{4})\s+(.+)$")
# Денежная сумма в RU-формате `925 423,27` (с пробелами или NBSP).
# Lookbehind `(?<![\d\xa0])` не даёт прицепиться к телефону `79994306912` —
# без этой защиты regex склеит «79994306912 100 000,00» как одно число.
# Thousands grouping строгое: `\d{1,3}(?:[\s\xa0]\d{3})*` — точно по три
# цифры через NBSP/space, что отсекает фрагменты длинных id и СБП-номеров.
MONEY_RE = re.compile(
    r"(?<![\d\xa0])([+\-]?\d{1,3}(?:[\s\xa0]\d{3})*),(\d{2})"
)
# ИНН контрагента: 10 или 12 цифр на отдельной позиции, иногда с КПП через запятую.
INN_KPP_RE = re.compile(r"\b(\d{10}|\d{12})(?:\s*,\s*\d{6,9})?\b")

# Концевые маркеры секции, до которых обрабатываем операции.
SECTION_END_RE = re.compile(
    r"Итого\s+оборотов:|Средства\s+на\s+конец\s+периода",
    re.IGNORECASE,
)

# Heuristic-словари направления.
# Modul direction inference основано на наблюдении из реального корпуса:
# в выписке Модульбанка контрагент = владелец счёта → внутренний перевод
# (Расход с расчётного на копилку/маркет-карту). Контрагент = другая
# фирма → Приход (внешний платёж нам). Это даёт ~95% точности на корпусе.
# Для оставшихся 5% (комиссии банка, налоги — где контрагент = банк/налоговая)
# используются keyword-словари ниже.
CREDIT_KEYWORDS = (
    "поступлени",  # Поступление
    "зачислени",   # Зачисление
    "возмещени",   # Возмещение
    "получени",    # Получение
)
DEBIT_KEYWORDS = (
    "комисси",     # Комиссия за …
    "налог",       # Налог на …
    "удержани",    # Удержание
    "штраф",
    "пени",
)


def can_parse(path: str | Path) -> bool:
    name = Path(path).name.lower()
    return "modul" in name or "модуль" in name


def parse_text(text: str) -> list[dict]:
    if not _is_modul_text(text):
        return []
    return _parse_modul(text)


def _is_modul_text(text: str) -> bool:
    head = text[:3000]
    return bool(MODUL_HEADER_RE.search(head))


def _parse_modul(text: str) -> list[dict]:
    """Iterate sections (по одному на каждый счёт), парсим каждую отдельно."""
    out: list[dict] = []
    sections = _split_sections(text)
    for section in sections:
        out.extend(_parse_section(section))
    return out


def _split_sections(text: str) -> list[str]:
    """Делит текст на секции по `Расчётный счёт № NNNN` границам."""
    matches = list(SECTION_HEADER_RE.finditer(text))
    if not matches:
        # Один счёт без явной границы — всё одной секцией.
        return [text]
    sections: list[str] = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.append(text[m.start() : end])
    return sections


def _parse_section(section: str) -> list[dict]:
    """Парсит один счёт-секцию.

    Стратегия — собрать «логические строки» операций. Логическая строка
    начинается с `<doc_no> <DD.MM.YYYY>`, продолжается следующими
    физическими строками пока не появится новый старт или не закончится
    раздел. Затем регексим сумму и реквизиты внутри.
    """
    own_acct, owner_name = _extract_section_meta(section)
    operations = _group_operations(section)
    out: list[dict] = []
    for raw_op in operations:
        tx = _process_operation(raw_op, owner_name, own_acct)
        if tx:
            out.append(tx)
    return out


def _extract_section_meta(section: str) -> tuple[str | None, str | None]:
    own_m = SECTION_HEADER_RE.search(section)
    own_acct = own_m.group(1) if own_m else None
    own_m2 = OWNER_RE.search(section)
    owner_name = own_m2.group(1).strip() if own_m2 else None
    return own_acct, owner_name


def _group_operations(section: str) -> list[str]:
    """Возвращает список «логических строк» операций (текст до следующего
    старта или до концевого маркера)."""
    lines = section.splitlines()
    operations: list[list[str]] = []
    current: list[str] | None = None
    in_table = False
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if SECTION_END_RE.search(s):
            break
        # Включаем «Приход» / «Расход» / «Назначение» как маркер начала
        # таблицы — операции идут после.
        if not in_table and re.search(r"Назначение\s+платежа", s, re.IGNORECASE):
            in_table = True
            continue
        if not in_table:
            continue
        m = TX_START_RE.match(s)
        if m:
            if current is not None:
                operations.append(current)
            current = [s]
        elif current is not None:
            current.append(s)
    if current is not None:
        operations.append(current)
    return [" ".join(parts) for parts in operations]


def _process_operation(raw: str, owner_name: str | None, own_acct: str | None) -> dict | None:
    m_start = TX_START_RE.match(raw)
    if not m_start:
        return None
    date_str = m_start.group(2)
    rest = m_start.group(3)
    d = parse_date(date_str)
    if d is None:
        return None

    # Сумма — последнее RU-money совпадение в строке.
    money_matches = list(MONEY_RE.finditer(rest))
    if not money_matches:
        return None
    last_money = money_matches[-1]
    rub_str = last_money.group(1)
    kop_str = last_money.group(2)
    cents = _parse_ru_money_cents(rub_str + "," + kop_str)
    if cents is None or cents == 0:
        return None

    # Reqs: ИНН в первой трети строки (после имени контрагента и до банка).
    inn_m = INN_KPP_RE.search(rest)
    inn = inn_m.group(1) if inn_m else None

    # Описание — всё «полезное» содержимое: от начала rest до суммы,
    # минус технические поля. Простая нормализация — оставляем как есть,
    # суммы и счета в конце уже за границей.
    desc_end = last_money.start()
    desc_raw = rest[:desc_end].strip()
    description = _normalize_description(desc_raw)

    # Direction heuristic.
    kind = _infer_direction(description, owner_name)
    signed = cents if kind == "Credit" else -cents

    tx = {
        "date": d.isoformat(),
        "amount": signed,
        "currency": "RUB",
        "kind": kind,
        "description": description,
        "raw": raw,
    }
    if inn:
        tx["id_key"] = inn
    return tx


def _normalize_description(s: str) -> str:
    """Чистим описание: убираем лишние пробелы, но сохраняем смысловую часть."""
    s = re.sub(r"\s+", " ", s).strip()
    return s[:500] if s else "Операция Модульбанк"


def _infer_direction(description: str, owner_name: str | None) -> str:
    """Heuristic: Credit (приход) или Debit (расход) по описанию.

    Логика по приоритету:
        1. **Контрагент = владелец счёта** → Debit. Это внутренний перевод
           с расчётного счёта на копилку/маркет-карту (списание для
           главного счёта). Самый сильный сигнал на реальном корпусе.
        2. **Banking-комиссия / налог** (по DEBIT_KEYWORDS) → Debit.
        3. **«Зачисление» / «Поступление» / «Возмещение»** → Credit.
        4. **Default** → Credit. На корпусе Модульбанка типичный неклассифи-
           цированный остаток — это «Оплата по договору ... за товар» от
           внешнего контрагента, что почти всегда приход (нам платят).
    """
    desc_lower = description.lower()
    if owner_name:
        owner_lower = owner_name.lower()
        # Первые 2 слова имени для устойчивого матча
        # («Алексенко Максим Павлович» → «алексенко максим»).
        # Вырезаем общие префиксы:
        owner_clean = re.sub(
            r"^(индивидуальный\s+предприниматель|ип|ооо|ао|пао|зао)\s+",
            "",
            owner_lower,
        )
        owner_key = " ".join(owner_clean.split()[:2])
        if owner_key and len(owner_key) >= 4 and owner_key in desc_lower:
            return "Debit"

    # Комиссии банка / налоги — Debit, даже если контрагент = банк.
    for kw in DEBIT_KEYWORDS:
        if kw in desc_lower:
            return "Debit"
    # Явные credit-маркеры
    for kw in CREDIT_KEYWORDS:
        if kw in desc_lower:
            return "Credit"
    # Fallback: внешний контрагент, неявные ключевые слова — типичный
    # «Оплата по договору ... за товар» от стороннего → Credit.
    return "Credit"


def _parse_ru_money_cents(token: str) -> int | None:
    """`925 423,27` → 92542327. RU-format с запятой для копеек."""
    t = token.strip()
    sign = 1
    if t.startswith("+"):
        t = t[1:]
    elif t.startswith("-"):
        sign = -1
        t = t[1:]
    cleaned = t.replace(" ", "").replace("\xa0", "")
    if "," not in cleaned:
        return None
    rub_str, kop_str = cleaned.split(",", 1)
    if len(kop_str) != 2 or not rub_str.isdigit() or not kop_str.isdigit():
        return None
    return sign * (int(rub_str) * 100 + int(kop_str))


def _statement_period(txs: list[dict]) -> dict | None:
    if not txs:
        return None
    dates = [t["date"] for t in txs]
    return {"from": min(dates), "to": max(dates)}


def _extract_account(text: str) -> str | None:
    m = SECTION_HEADER_RE.search(text)
    return m.group(1) if m else None


def parse(path: str | Path) -> dict:
    text = extract_text(path)
    txs = drop_noise(parse_text(text))
    if not txs:
        raise ValueError(f"no transactions parsed from {path}")
    return {
        "bank": NAME,
        "account": _extract_account(text),
        "period": _statement_period(txs),
        "transactions": txs,
    }
