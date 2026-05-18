"""Raiffeisenbank statement parser (Python port).

Port of crates/bank-parsers/src/banks/raif.rs.

Поддерживаются два формата:

1. **Legacy** («Справка об операциях по счету»). Транзакции разделены
   границами `DD.MM.YYYY HH:MM` (с временем), описание следует за двумя
   суммами с маркером `₽`.

2. **Tabular retail** («Выписка по счету дебетовой карты»). Табличная
   раскладка с колонками `Дата проводки | Дата операции | Детали операции |
   Валюта | Сумма операции | Сумма в валюте счёта`. Каждая транзакция —
   одна логическая строка вида:

       02.07.2025 02.07.2025 Current exp Jan2025 RUB 770,000.00 770,000.00
       07.07.2025 04.07.2025 Перевод клиенту ЮниКредит Банк RUB 40,303.56 -40,303.56

   Описание может быть многострочным — например, «Выплата заработной
   платы. НДС не облагается» переносится на 2 строки. Парсер склеивает
   их в одну строку и применяет regex.

Маршрутизация: `_is_tabular_format(text)` проверяет наличие маркера
«Дата проводки» в первых ~30 строках; если есть — `_parse_tabular`,
иначе `_parse_legacy`.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from .common import drop_noise, extract_inn
from .pdf import extract_text

NAME = "Raif"

TX_BOUNDARY_RE = re.compile(r"\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}")
DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")
RAIF_AMOUNT_RE = re.compile(r"([+\-])\s*([\d][\d\s]*),(\d{2})\s*₽")

# Tabular retail format
TABULAR_HEADER_RE = re.compile(
    r"Дата\s+проводки\s+Дата\s+операции",
    re.IGNORECASE,
)
TABULAR_TX_RE = re.compile(
    r"(\d{2}\.\d{2}\.\d{4})\s+"           # Дата проводки
    r"(\d{2}\.\d{2}\.\d{4})\s+"           # Дата операции
    r"(.+?)\s+"                            # Описание (non-greedy)
    r"(RUB|USD|EUR|RUR)\s+"               # Валюта
    r"([\d,]+\.\d{2})\s+"                 # Сумма операции
    r"(-?[\d,]+\.\d{2})"                  # Сумма в валюте счёта (signed)
    r"(?=\s|$)",
    re.DOTALL,
)
# Линии-итоги, которые могут попасть в склейку и сломать парсинг.
TABULAR_SUMMARY_RE = re.compile(
    r"\b(Поступления|Списания|Исходящий остаток|Входящий остаток|Всего\b)",
    re.IGNORECASE,
)
NOISE_RE = re.compile(
    r"(?:Стр\.\s*\d+\s*из\s*\d+"
    r"|Продолжение на следующей странице"
    r"|Дата выдачи:[^А-Яа-яA-Za-z]*\d{2}\.\d{2}\.\d{4}[^А-Яа-яA-Za-z]*(?:года)?[^А-Яа-яA-Za-z]*\d{2}:\d{2}\s*МСК"
    r"|Номер счета:\s*[\d\s]+"
    r"|Дата операцииВыполнена банкомНомердокументаСумма в валютеоперацииСумма в валютесчетаДетали операцииНомеркарты"
    r"|Информация в справке актуальна на дату выдачи"
    r"|Руководитель.*"
    r"|Всего поступлений.*"
    r"|Всего расходов.*)"
)
TAIL_JUNK_RE = re.compile(
    r"(?:Телефон получателя\s*\d+"
    r"|Идентификатор операции\s*(?:СБП\s*)?\S+"
    r"|Банк получателя[^.]*"
    r"|Сообщение\s*$)"
)
ATM_RE = re.compile(r"ATM\s+\d+\s+\S+")
DOC_CODE_RE = re.compile(r"\b(?:ZP[A-Z0-9]+|[A-Z]{1,3}\d{6,}[A-Z0-9]*|[A-Z]{2}\d+[A-Z]\w{4,})\b")
ACCOUNT_RE = re.compile(r"4[\d\s]{18,22}[\d]")


def _parse_date_str(s: str) -> date | None:
    m = DATE_RE.search(s)
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def _parse_raif_amount(m: re.Match) -> int | None:
    sign = 1 if m.group(1) == "+" else -1
    int_part = "".join(c for c in m.group(2) if c.isdigit())
    frac = m.group(3)
    if not int_part or not frac:
        return None
    return sign * (int(int_part) * 100 + int(frac))


def _clean_description(raw: str) -> str:
    s = ATM_RE.sub("Снятие наличных", raw)
    s = TAIL_JUNK_RE.sub("", s)
    s = DOC_CODE_RE.sub("", s)
    s = " ".join(s.split())
    s = s.strip(" .,")
    return s if len(s) >= 3 else "Операция Райффайзен"


def _split_segments(text: str) -> list[str]:
    bounds = [m.start() for m in TX_BOUNDARY_RE.finditer(text)]
    if not bounds:
        return []
    out = []
    for i, b in enumerate(bounds):
        end = bounds[i + 1] if i + 1 < len(bounds) else len(text)
        out.append(text[b:end])
    return out


def _process_segment(seg: str) -> dict | None:
    date_m = DATE_RE.search(seg)
    if not date_m:
        return None
    d = _parse_date_str(date_m.group(0))
    if d is None:
        return None
    matches = list(RAIF_AMOUNT_RE.finditer(seg))
    if not matches:
        return None
    cents = _parse_raif_amount(matches[0])
    if cents is None:
        return None
    desc_start = matches[1].end() if len(matches) >= 2 else matches[0].end()
    raw_desc = seg[desc_start:]
    cleaned_noise = NOISE_RE.sub(" ", raw_desc)
    description = _clean_description(cleaned_noise)
    return {
        "date": d.isoformat(),
        "amount": cents,
        "currency": "RUB",
        "kind": "Credit" if cents >= 0 else "Debit",
        "description": description,
        "raw": seg,
    }


def parse_text(text: str) -> list[dict]:
    """Public entry: маршрутизирует на табличный или legacy-парсер."""
    if _is_tabular_format(text):
        out = _parse_tabular(text)
        if out:
            return out
        # Fallback: если табличный не нашёл транзакций (формат изменился),
        # пробуем legacy — лучше получить хоть что-то.
    out = []
    for seg in _split_segments(text):
        row = _process_segment(seg)
        if row:
            out.append(row)
    return out


def _is_tabular_format(text: str) -> bool:
    """True если текст содержит маркер табличной раскладки в первых
    ~30 непустых строках (после ФИО, шапки)."""
    head = text[:3000]
    return bool(TABULAR_HEADER_RE.search(head))


def _parse_tabular(text: str) -> list[dict]:
    """Парсер табличного retail-формата.

    Стратегия:
      1. Находим точку входа в раздел операций (после `Дата проводки`).
      2. Склеиваем все строки до строки-итога (`Поступления`/`Списания`)
         в один длинный текст с пробелами вместо переводов.
      3. Прогоняем regex `TABULAR_TX_RE` по склеенному тексту — получаем
         ноль или больше совпадений, каждое = одна транзакция.

    Многострочные описания корректно собираются благодаря склейке в шаге 2.
    """
    lines = text.splitlines()

    # Найти точку входа — строку с маркером.
    start = 0
    for i, ln in enumerate(lines):
        if TABULAR_HEADER_RE.search(ln):
            start = i + 1
            break
    if start == 0:
        return []

    # Склеить строки до первой строки-итога. На реальных файлах раздел
    # операций — между «Дата проводки …» и «Поступления / Списания /
    # Исходящий остаток». Но «Поступления» иногда встречается и в
    # середине таблицы (в формате видно `Поступления 1,068,576.65` после
    # последней операции, но до `Списания`/`Исходящий остаток`). Берём
    # всё до Исходящего остатка чтобы не упустить транзакции.
    body_parts: list[str] = []
    for ln in lines[start:]:
        s = ln.strip()
        if not s:
            continue
        if "Исходящий остаток" in s:
            break
        body_parts.append(s)
    body = " ".join(body_parts)

    # Удалим из тела строки-итоги (они matcheлись бы в regex как описание
    # и съели бы соседние tx). Исходящий остаток уже отрезан выше.
    body = re.sub(
        r"(Поступления|Списания)\s+[\d,]+\.\d{2}",
        " ",
        body,
        flags=re.IGNORECASE,
    )

    out: list[dict] = []
    for m in TABULAR_TX_RE.finditer(body):
        d_str = m.group(1)
        currency = m.group(4).upper()
        if currency == "RUR":
            currency = "RUB"
        signed_str = m.group(6)
        # `1,068,576.65` → 1068576.65 → копейки 106857665.
        cents = _parse_us_money_cents(signed_str)
        if cents is None:
            continue
        d = _parse_date_str(d_str)
        if d is None:
            continue
        description = " ".join(m.group(3).split()).strip()
        if not description:
            description = "Операция Райффайзен"
        tx = {
            "date": d.isoformat(),
            "amount": cents,
            "currency": currency,
            "kind": "Credit" if cents >= 0 else "Debit",
            "description": description,
            "raw": m.group(0),
        }
        id_key = extract_inn(description)
        if id_key:
            tx["id_key"] = id_key
        out.append(tx)
    return out


def _parse_us_money_cents(s: str) -> int | None:
    """Парсит US-стиль `1,068,576.65` или `-300.00` → копейки. Sign preserved."""
    t = s.strip()
    sign = 1
    if t.startswith("-"):
        sign = -1
        t = t[1:]
    elif t.startswith("+"):
        t = t[1:]
    cleaned = t.replace(",", "")
    if "." not in cleaned:
        return None
    rub_str, kop_str = cleaned.split(".", 1)
    if not rub_str.isdigit() or len(kop_str) != 2 or not kop_str.isdigit():
        return None
    return sign * (int(rub_str) * 100 + int(kop_str))


def _extract_account(text: str) -> str | None:
    m = ACCOUNT_RE.search(text)
    if not m:
        return None
    digits = "".join(c for c in m.group(0) if c.isdigit())
    return digits if len(digits) == 20 else None


def can_parse(path: str | Path) -> bool:
    name = Path(path).name.lower()
    return "raif" in name or "райф" in name or "raiffeisen" in name


def _statement_period(txs: list[dict]) -> dict | None:
    if not txs:
        return None
    dates = [t["date"] for t in txs]
    return {"from": min(dates), "to": max(dates)}


def parse(path: str | Path) -> dict:
    text = extract_text(path)
    # Пускаем по тексту — Райф может встречаться как «Райффайзен»,
    # «Raiffeisen», или (в табличной retail-форме) только как `RAIFFEISEN`
    # в названии банкомата + общая Латиница без названия банка в шапке.
    has_raif_marker = (
        "Райффайзен" in text
        or "Raiff" in text
        or "RAIFFEISEN" in text
        or _is_tabular_format(text)  # табличный retail тоже считаем за Raif
    )
    if not has_raif_marker:
        raise ValueError(f"не похоже на выписку Райффайзенбанка: {path}")
    # Табличный retail-формат может содержать описания вида «Капитализация:
    # входящий остаток …, …, …»; balance-группа здесь — false-positive.
    drop_balance = not _is_tabular_format(text)
    txs = drop_noise(parse_text(text), balance=drop_balance)
    return {
        "bank": NAME,
        "account": _extract_account(text),
        "period": _statement_period(txs),
        "transactions": txs,
    }
