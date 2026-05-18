"""Shared parsing helpers for all bank parsers.

Mirrors crates/bank-parsers/src/common.rs. Keep behaviour identical: snapshot
tests compare Python output to the Rust baseline byte-for-byte after sorting.
"""

from __future__ import annotations

import re
from datetime import date

# Шумовые паттерны разделены на 3 группы. Глобальный фильтр исторически
# слепо проверял все три — но это давало false-positives на форматах, где
# слова «открытие счёта» / «входящий остаток» — не сводки, а легитимные
# операции (Sber-deposit «Открытие вклада», Раифовские формулы).
#
# Парсер опционально отключает группы через kwargs `drop_noise()`. Default
# поведение (drop everything) сохранено для backwards-compat с уже
# мигрированными парсерами.

# Group 1: безусловная сводка. Эти строки никогда не бывают операциями ни
# в одном банке. Их всегда дропаем.
SUMMARY_NOISE: tuple[str, ...] = (
    "итого", "всего по выписке", "всего операций",
    "оборот за период", "обороты по счёту", "обороты по счету",
    "сумма пополнений", "сумма списаний",
    "пополнения за период", "расходы за период",
    "поступления за период", "выплаты за период",
    "балансовый итог",
    "перенос", "перенесено", "продолжение на следующей странице", "продолжение",
    "carried forward", "brought forward",
    "opening balance", "closing balance", "subtotal",
)

# Group 2: «остаток / баланс». Чаще всего это shaped-сводка («остаток на
# 31.08»), но в формуле капитализации Сбер-вклада и Райф-табличного формата
# слова «входящий остаток» появляются как часть длинного описания. Парсеры
# этих форматов отключают эту группу.
BALANCE_LINES: tuple[str, ...] = (
    "остаток на", "остаток по счёту", "остаток по счету", "остаток средств",
    "входящий остаток", "исходящий остаток",
    "входящее сальдо", "исходящее сальдо", "сальдо на",
    "баланс на", "balance", "доступная сумма", "доступно средств",
)

# Group 3: lifecycle счёта. Для карточных и расчётных выписок это сводка
# («Закрытие счёта на дату X»), для депозитных — реальная операция
# («Открытие вклада», «Закрытие вклада с выплатой процентов»). Sber-deposit
# отключает эту группу, остальные парсеры — нет.
ACCOUNT_LIFECYCLE: tuple[str, ...] = (
    "закрытие счёта", "закрытие счета", "открытие счёта", "открытие счета",
)

# Backwards-compat алиас. Старые callers могут импортировать
# `NOISE_PATTERNS` напрямую — сохраняем как объединение всех трёх групп.
NOISE_PATTERNS: tuple[str, ...] = (
    *SUMMARY_NOISE, *BALANCE_LINES, *ACCOUNT_LIFECYCLE, "total",
)


def is_noise(description: str, patterns: tuple[str, ...] = NOISE_PATTERNS) -> bool:
    """Описание сводное (а не операция)?"""
    s = description.strip().lower()
    if not s:
        return True
    return any(p in s for p in patterns)


def drop_noise(
    transactions: list[dict],
    *,
    summary: bool = True,
    balance: bool = True,
    lifecycle: bool = True,
) -> list[dict]:
    """Drop summary/balance/lifecycle строки из списка транзакций.

    Default: drop all three categories — bequest behaviour, эквивалентен
    старому `drop_noise(txs)`. Парсер может выключить отдельные группы:

      - `balance=False` — для форматов с «входящий остаток» в формуле
        капитализации (Sber-deposit, Raif-tabular).
      - `lifecycle=False` — для депозитных форматов, где «Открытие вклада»
        и «Закрытие вклада» — это реальные операции (Sber-deposit).
    """
    patterns: list[str] = []
    if summary:
        patterns.extend(SUMMARY_NOISE)
        patterns.append("total")  # рисковато для merchant names, но
                                  # исторически фильтровалось — оставляем
                                  # в summary-группе для back-compat.
    if balance:
        patterns.extend(BALANCE_LINES)
    if lifecycle:
        patterns.extend(ACCOUNT_LIFECYCLE)
    return [t for t in transactions if not is_noise(t["description"], tuple(patterns))]


DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")
# `(?<![\d*])` blocks the same overflow that bit Alfa: a card number tail
# like `****9446 1 000,00 ₽` would otherwise let `\d[\d\s ]*,\d{2}` eat the
# `9446` as the integer part of the next amount. Mirror of the lookbehind
# used in `bank_parsers.alfa.AMOUNT_RUR_RE`.
MONEY_RE = re.compile(r"(?<![\d*])([+\-]?\d[\d\s ]*),(\d{2})")
HEAD_JUNK_RE = re.compile(r"\b\d{2}:\d{2}\b|\b\d{6,8}\b")

# Извлечение последних 4 цифр карты — единый regex для всех python-парсеров,
# зеркало `crates/bank-parsers/src/common.rs::CARD_AFTER_LABEL_RE`.
# Маркер обязателен (карта|MasterCard|*|•) — чтобы не поймать год или сумму.
CARD_LAST4_RE = re.compile(
    r"(?:[Кк]арт[аеу]|MasterCard|Visa|Maestro|МИР|MIR|"
    r"\*{2,4}|•{2,4})"
    r"[^\d\n]{0,12}(\d{4})\b"
)


def extract_card_last4(text: str, scan_chars: int = 1500) -> str | None:
    """Ищет последние 4 цифры карты в первых `scan_chars` символах текста.
    Возвращает None если маркера карты нет."""
    if not text:
        return None
    head = text[:scan_chars]
    m = CARD_LAST4_RE.search(head)
    return m.group(1) if m else None


def parse_date(s: str) -> date | None:
    m = DATE_RE.search(s)
    if not m:
        return None
    try:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return date(y, mo, d)
    except ValueError:
        return None


def parse_money_cents(token: str) -> int | None:
    """'1 234,56' -> 123456 kopecks. Sign preserved.

    Mirror of Rust common::parse_money_cents.
    """
    t = token.strip()
    sign = 1
    if t.startswith("+"):
        t = t[1:]
    elif t.startswith("-"):
        sign = -1
        t = t[1:]
    cleaned = "".join(c for c in t if not c.isspace())
    if "," not in cleaned:
        return None
    rub_str, kop_str = cleaned.split(",", 1)
    if len(kop_str) != 2 or not rub_str.isdigit() or not kop_str.isdigit():
        return None
    return sign * (int(rub_str) * 100 + int(kop_str))


def split_lines(text: str) -> list[str]:
    out = []
    for ln in text.splitlines():
        s = ln.strip()
        if s and s.lower() != "nan":
            out.append(s)
    return out


# ── id_key extraction ────────────────────────────────────────────────────

# ИНН: 10 цифр (юр. лицо) или 12 цифр (ИП/физлицо). Окружаем границами
# слова, чтобы не зацепиться за номер счёта (20 цифр).
_INN_RE = re.compile(r"(?<!\d)(\d{10}|\d{12})(?!\d)")

# Маркеры «ИНН: <число>» / «ИНН плательщика: <число>» — приоритетнее
# чем просто 10/12-цифровое число, потому что в описании могут быть
# номера документов, телефоны и пр.
_INN_LABELED_RE = re.compile(
    r"\bИНН(?:\s+(?:плательщика|получателя|отправителя))?\s*[:№]?\s*(\d{10}|\d{12})\b",
    re.IGNORECASE,
)

# Чёрный список «фейк-ИНН» — последовательности нулей или «12345...»,
# которые встречаются в шаблонах документов.
_FAKE_INN = frozenset({
    "0" * 10, "0" * 12,
    "1234567890", "123456789012",
})


def extract_inn(description: str) -> str | None:
    """Извлекает ИНН (10 или 12 цифр) из описания транзакции.

    Стратегия:
        1. Сначала ищем явный маркер `ИНН: NNNN` — это самый надёжный
           вариант (плательщик/получатель структурно указан).
        2. Если не нашёлся — ищем любое 10/12-цифровое число с границами
           слова. Берём первое; если попадётся номер счёта (20 цифр),
           lookbehind/lookahead отсекут его.

    Возвращает строку с ИНН или `None` если не нашлось.

    Примеры:
        >>> extract_inn("Оплата ООО Ромашка ИНН 7707083893")
        '7707083893'
        >>> extract_inn("Перевод 12345 ИП Иванов ИНН: 771234567890")
        '771234567890'
        >>> extract_inn("Перевод СБП Иванов И.")
        None
    """
    if not description:
        return None
    # 1. Явный маркер «ИНН: …»
    m = _INN_LABELED_RE.search(description)
    if m and m.group(1) not in _FAKE_INN:
        return m.group(1)
    # 2. Первое 10/12-цифровое число с границами
    for m in _INN_RE.finditer(description):
        candidate = m.group(1)
        if candidate in _FAKE_INN:
            continue
        return candidate
    return None


# ИП Иванов / ООО «Ромашка» / ОАО «Газпром» — для PER/ORG fallback,
# когда ИНН не найден. Допускаем пробелы, дефисы, точки в имени
# («Иванов И. И.», «ТД-Сервис»).
_ORG_RE = re.compile(
    r"\b(?:ООО|ОАО|АО|ИП|ПАО|ЗАО|НКО|ОПФР|УФК)\s+[«\"']?"
    r"([А-ЯЁA-Z][\wА-ЯЁа-яё«»\"' \-\.]{2,40})",
)


def extract_org_or_person(description: str) -> str | None:
    """Извлекает имя организации (после «ООО»/«ИП»/...) или ФИО.

    Не строгая регулярка — это fallback на случай отсутствия ИНН.
    Возвращает первое совпадение, обрезанное до ~40 символов.
    """
    if not description:
        return None
    m = _ORG_RE.search(description)
    if m:
        name = m.group(1).strip(' "«»\'\t')
        # Сократим до 50 символов
        return name[:50] if name else None
    return None
