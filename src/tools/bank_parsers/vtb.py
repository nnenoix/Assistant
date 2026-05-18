"""VTB statement parser (Python port).

Port of crates/bank-parsers/src/banks/vtb.rs. Поддерживаются два формата:

1. **Personal/Retail** («Номер счёта» / «Период выписки»). Каждая
   транзакция — небольшой блок: дата, время+сумма, RUB-колонки, описание.

2. **Corporate B2B** («ВЫПИСКА за период с ... по ...»). Многострочный
   табличный формат с колонками:

       Дата № ВО  ИНН   БИК   Счёт   Наименование  Дебет  Кредит  Назначение

   Строка-старт блока (одна строка):
       01.01.2026 314345 17 7702070139 044525411 47422810206474003208 ФИЛИАЛ

   ↓ многострочное имя:
       "ЦЕНТРАЛЬНЫЙ"
       БАНКА ВТБ (ПАО)

   ↓ amount-line + начало назначения:
       1 480.00 0.00 Оплата стоимости пакета услуг "Самое важное"

   ↓ многострочное назначение:
       за период с 01/01/2026 по 31/01/2026 согласно
       тарифам Банка (п. 17.1.3.). НДС не облагается.

   Блок заканчивается перед следующим Block-START. ИНН (поле 4) идёт
   структурно — поэтому corp-формат сразу даёт `id_key` без NER.

Маршрутизация: `_is_corp_format(text)` ищет «ВЫПИСКА за период с»;
если есть — `_parse_corp_text`, иначе старый Format A (personal).
"""

from __future__ import annotations

import re
from pathlib import Path

from .common import drop_noise, extract_inn, parse_date, split_lines
from .pdf import extract_text

NAME = "Vtb"

# Corp Format-B markers / regexes
CORP_HEADER_RE = re.compile(r"ВЫПИСКА\s+за\s+период\s+с", re.IGNORECASE)
CORP_OWN_ACCOUNT_RE = re.compile(r"Счет\s+(\d{20})", re.IGNORECASE)
CORP_PERIOD_RE = re.compile(
    r"ВЫПИСКА\s+за\s+период\s+с\s+(\d{2}\.\d{2}\.\d{4})\s+по\s+(\d{2}\.\d{2}\.\d{4})",
    re.IGNORECASE,
)
# Block-start line. Все 6 структурных колонок в одной строке: дата,
# № документа, ВО (1-2 цифры), ИНН (10/12), БИК (9), счёт (20). Хвост —
# начало наименования контрагента.
CORP_BLOCK_START_RE = re.compile(
    r"^(\d{2}\.\d{2}\.\d{4})\s+"           # дата
    r"(\d+)\s+"                              # № документа
    r"(\d{1,3})\s+"                          # ВО (вид операции)
    r"(\d{10}|\d{12})\s+"                    # ИНН контрагента
    r"(\d{9})\s+"                            # БИК банка
    r"(\d{20})\s+"                           # счёт
    r"(.+)$"                                 # начало наименования
)
# Amount-line: «1 480.00 0.00 [optional first line of purpose]».
# Точка как разделитель копеек (US-стиль), пробелы или NBSP (`\xa0`) —
# разделители тысяч. PDF-extract Outputs NBSP в банковских PDF — ловим обе
# формы через `[\d \xa0]+`.
CORP_AMOUNT_LINE_RE = re.compile(
    r"^([\d\s\xa0]+\.\d{2})\s+([\d\s\xa0]+\.\d{2})(?:\s+(.+))?$"
)
# Inline-форма (всё в одной строке): block-start + amounts + purpose
# уехали на одну линию — встречается когда наименование короткое.
CORP_INLINE_RE = re.compile(
    r"^(\d{2}\.\d{2}\.\d{4})\s+"
    r"(\d+)\s+(\d{1,3})\s+(\d{10}|\d{12})\s+(\d{9})\s+(\d{20})\s+"
    r"(.+?)\s+([\d\s\xa0]+\.\d{2})\s+([\d\s\xa0]+\.\d{2})(?:\s+(.+))?$"
)

DATE_ONLY_LINE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
STARTS_WITH_TIME_RE = re.compile(r"^\d{2}:\d{2}:\d{2}")
AMOUNT_RUB_RE = re.compile(r"(-?\d[\d,]*\.\d{2}) RUB")
RUB_NUMBER_LINE_RE = re.compile(r"^RUB\s+[\d,\.]+$")
COMMISSION_LINE_RE = re.compile(r"^\d[\d,]*\.\d{2}(?:\s+(.*))?$")
ACCOUNT_RE = re.compile(r"(?:Номер счёта|Счет)\s+(\d{20})")


def _parse_amount_rub(line: str) -> int | None:
    m = AMOUNT_RUB_RE.search(line)
    if not m:
        return None
    s = m.group(1)
    sign = -1 if s.startswith("-") else 1
    cleaned = s.lstrip("-").replace(",", "")
    if "." not in cleaned:
        return None
    rub_str, kop_str = cleaned.split(".", 1)
    try:
        return sign * (int(rub_str) * 100 + int(kop_str))
    except ValueError:
        return None


def _is_format_a(lines: list[str]) -> bool:
    for line in lines[:30]:
        if "Номер счёта" in line or "Период выписки" in line or "Операции по счёту" in line:
            return True
    for line in lines:
        if AMOUNT_RUB_RE.search(line):
            return True
    return False


def _parse_format_a(lines: list[str]) -> list[dict]:
    n = len(lines)
    out: list[dict] = []
    i = 0
    while i + 1 < n:
        date_line = lines[i].strip()
        next_line = lines[i + 1].strip()
        if not DATE_ONLY_LINE_RE.match(date_line):
            i += 1
            continue
        if not STARTS_WITH_TIME_RE.match(next_line):
            i += 1
            continue
        d = parse_date(date_line)
        if d is None:
            i += 1
            continue
        cents = _parse_amount_rub(next_line)
        if cents is None:
            i += 2
            continue

        j = i + 2
        desc_parts: list[str] = []
        found_commission = False
        while j < n:
            lj = lines[j].strip()
            if (
                j + 1 < n
                and DATE_ONLY_LINE_RE.match(lj)
                and STARTS_WITH_TIME_RE.match(lines[j + 1].strip())
            ):
                break
            if RUB_NUMBER_LINE_RE.match(lj):
                j += 1
                continue
            if lj == "RUB" or lj == "0":
                j += 1
                continue
            no_cd = lj.replace(",", "").replace(".", "")
            if no_cd and no_cd.isdigit():
                j += 1
                continue
            if not found_commission:
                m = COMMISSION_LINE_RE.match(lj)
                if m:
                    found_commission = True
                    rest = m.group(1) or ""
                    text = rest.strip()
                    if text.startswith("RUB "):
                        text = text[4:].strip()
                    if text:
                        desc_parts.append(text)
                    j += 1
                    continue
                if lj.startswith("RUB "):
                    rest = lj[4:].strip()
                    if rest:
                        found_commission = True
                        desc_parts.append(rest)
                    else:
                        j += 1
                        continue
                else:
                    found_commission = True
                    desc_parts.append(lj)
            else:
                text = lj[4:].strip() if lj.startswith("RUB ") else lj
                if text:
                    desc_parts.append(text)
            j += 1

        description = " ".join(desc_parts).strip() or "Неизвестно"
        raw_block = " ".join(lines[i:j]) if j <= n else " ".join(lines[i:])
        tx = {
            "date": d.isoformat(),
            "amount": cents,
            "currency": "RUB",
            "kind": "Credit" if cents >= 0 else "Debit",
            "description": description,
            "raw": raw_block,
        }
        # VTB-personal: ИНН в `description` редко встречается (СБП-переводы
        # дают только имя), но иногда «Перечисление подотчётному лицу ...
        # ИНН 7707…» появляется. Если найдём — кладём.
        id_key = extract_inn(description)
        if id_key:
            tx["id_key"] = id_key
        out.append(tx)
        i = j
    return out


def parse_text(text: str) -> list[dict]:
    """Public entry: маршрутизирует на corp-, retail- или legacy-парсер."""
    if _is_corp_format(text):
        out = _parse_corp_text(text)
        if out:
            return out
        # fallback: corp-detect был ложно-положительным, пробуем retail
    lines = split_lines(text)
    return _parse_format_a(lines) if _is_format_a(lines) else []


# ── Corp Format-B ─────────────────────────────────────────────────────────


def _is_corp_format(text: str) -> bool:
    """Маркер B2B-выписки — «ВЫПИСКА за период с …» в первых ~50 строках."""
    head = text[:5000]
    return bool(CORP_HEADER_RE.search(head))


def _parse_corp_text(text: str) -> list[dict]:
    """Walks lines using a state machine.

    Состояния:
        SEEKING        — ищем block-start
        IN_BLOCK_NAME  — block-start найден, копим строки наименования
                          до amount-line
        IN_BLOCK_DESC  — amount-line найден, копим строки назначения
                          до следующего block-start
    """
    lines = [ln.rstrip() for ln in text.splitlines()]
    out: list[dict] = []

    block_date: str = ""
    block_inn: str = ""
    block_name_parts: list[str] = []
    block_amount: int = 0  # cents, signed
    block_purpose_parts: list[str] = []
    state = "SEEKING"

    def emit() -> None:
        if not block_date or block_amount == 0:
            return
        name = " ".join(p.strip() for p in block_name_parts if p.strip())
        purpose = " ".join(p.strip() for p in block_purpose_parts if p.strip())
        # Описание = «<имя> — <назначение>». Если name пустое (не должно
        # быть в реальном corp-формате), оставляем только purpose.
        if name and purpose:
            description = f"{name} — {purpose}"
        elif name:
            description = name
        elif purpose:
            description = purpose
        else:
            description = "Корпоративная операция ВТБ"
        # Trim до разумной длины; типовая 1С обрезает до 210 символов.
        description = description[:500]
        d = parse_date(block_date)
        if d is None:
            return
        tx = {
            "date": d.isoformat(),
            "amount": block_amount,
            "currency": "RUB",
            "kind": "Credit" if block_amount >= 0 else "Debit",
            "description": description,
            "raw": None,
        }
        # ИНН структурно есть — это и есть `id_key`. ИНН из формата всегда
        # 10/12 цифр контрагента (не наш счёт), что и нужно для ABC.
        if block_inn:
            tx["id_key"] = block_inn
        out.append(tx)

    def _start_block(m: re.Match[str]) -> None:
        nonlocal block_date, block_inn, block_name_parts, block_amount, block_purpose_parts
        # Если в текущем буфере блок незавершённый — emit'нуть его.
        emit()
        block_date = m.group(1)
        block_inn = m.group(4)
        # ИНН VTB (7702070139) = это сам банк, а не контрагент. Внутрибанковские
        # фильтры (как в onec-export::is_internal_account) не релевантны для
        # ИНН — но для аналитики ИНН банка-эмитента бесполезен. Пропускаем
        # его (id_key = None, NER подхватит) если контрагент — банк.
        if block_inn == "7702070139":
            block_inn = ""
        block_name_parts = [m.group(7)]
        block_amount = 0
        block_purpose_parts = []

    for line in lines:
        s = line.strip()
        if not s:
            continue

        # Inline-форма: всё в одной строке. Сразу emit и start-end.
        m_inline = CORP_INLINE_RE.match(s)
        if m_inline:
            emit()
            block_date = m_inline.group(1)
            inn = m_inline.group(4)
            block_inn = "" if inn == "7702070139" else inn
            block_name_parts = [m_inline.group(7)]
            debit_str = m_inline.group(8)
            credit_str = m_inline.group(9)
            block_amount = _corp_signed_cents(debit_str, credit_str)
            block_purpose_parts = [m_inline.group(10) or ""]
            emit()
            block_date = ""
            block_inn = ""
            block_name_parts = []
            block_amount = 0
            block_purpose_parts = []
            state = "SEEKING"
            continue

        m_start = CORP_BLOCK_START_RE.match(s)
        if m_start:
            _start_block(m_start)
            state = "IN_BLOCK_NAME"
            continue

        if state == "IN_BLOCK_NAME":
            m_amt = CORP_AMOUNT_LINE_RE.match(s)
            if m_amt:
                debit_str = m_amt.group(1)
                credit_str = m_amt.group(2)
                block_amount = _corp_signed_cents(debit_str, credit_str)
                first_purpose = m_amt.group(3)
                if first_purpose:
                    block_purpose_parts = [first_purpose]
                state = "IN_BLOCK_DESC"
                continue
            # Иначе — продолжение наименования.
            block_name_parts.append(s)
            continue

        if state == "IN_BLOCK_DESC":
            # Продолжение назначения. Block завершится на следующем
            # block-start (обработан выше).
            block_purpose_parts.append(s)
            continue

        # state == "SEEKING" и строка не block-start — игнорим (header
        # таблицы / шапка документа / итоговые строки).

    # Emit последний блок если был открыт.
    emit()
    return out


def _corp_signed_cents(debit_str: str, credit_str: str) -> int:
    """Конвертит дебет/кредит в подписанные копейки.

    Дебет ≠ 0  → отрицательная сумма (списание со счёта)
    Кредит ≠ 0 → положительная сумма (зачисление)
    Оба ≠ 0 встречается редко (двусторонние корректировки) — берём
    разницу credit - debit как net.
    """
    debit = _corp_parse_money(debit_str) or 0
    credit = _corp_parse_money(credit_str) or 0
    return credit - debit


def _corp_parse_money(s: str) -> int | None:
    """`1 480.00` → 148000. Поддерживает US-decimal с точкой; разделители
    тысяч могут быть пробелом или NBSP (`\xa0` — typical для PDF-extract)."""
    t = s.strip().replace(" ", "").replace("\xa0", "")
    if "." not in t:
        return None
    rub_str, kop_str = t.split(".", 1)
    if not rub_str.isdigit() or len(kop_str) != 2 or not kop_str.isdigit():
        return None
    return int(rub_str) * 100 + int(kop_str)


def _extract_account(text: str) -> str | None:
    m = ACCOUNT_RE.search(text)
    if m:
        return m.group(1)
    for line in text.splitlines():
        t = line.strip()
        if len(t) == 20 and t.isdigit():
            return t
    return None


def can_parse(path: str | Path) -> bool:
    name = Path(path).name.lower()
    return (
        "vtb_bankstatement" in name
        or "втб" in name
        or name.startswith("vtb")
        or "vtb" in name
        or ("выписк" in name and ("408178" in name or "40817" in name))
    )


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
        "account": _extract_account(text),
        "period": _statement_period(txs),
        "transactions": txs,
    }
