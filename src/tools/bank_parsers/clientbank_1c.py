"""1CClientBankExchange (.txt) importer.

Это **не банк**, а формат, в котором 1С (или любая внешняя ERP, говорящая на
этом языке) сохраняет уже распарсенные документы. Combo принимает такой файл
как первоисточник для аналитики:

  - ИНН плательщика/получателя есть структурно → ABC-аналитика сразу
    группирует контрагентов точно, без NER.
  - НазначениеПлатежа есть отдельным полем → не нужно вытаскивать из «сырого»
    описания.
  - Период и р/с тоже есть в шапке.

Спецификация:
  - Кодировка Windows-1251 (CP1251). Файл может приходить и в UTF-8 / UTF-8
    BOM — мы пробуем оба варианта.
  - Перевод строк CRLF, но строки с одиночными `\\n` тоже допускаются.
  - Структура:
        1CClientBankExchange
        Ключ=Значение
        ...
        СекцияДокумент=Платежное поручение
        Ключ=Значение
        ...
        КонецДокумента
        ...
        КонецФайла
  - Маркеры (`СекцияДокумент`, `КонецДокумента`, `КонецФайла`) могут идти
    как с `=значение`, так и без `=` — старые версии 1.01/1.02 пишут
    `СекцияДокумент=Платежное поручение`, новая 1.03 — то же.
  - `Сумма=12354.67` — точка как разделитель копеек. Допустимо `12354,67`
    (старые БСП) и `12354` (целое число рублей).
  - `Дата=ДД.ММ.ГГГГ`.
  - Пустые поля не выводятся (например, `ПлательщикКПП=` отсутствует, если
    плательщик ИП).

Возвращаемая структура — стандартный `Statement` (см. combo-core::Statement),
тот же контракт что и у PDF-парсеров.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import TypedDict

NAME = "ClientBank1C"

# ────────────────────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────────────────────

# Магическая строка в самом начале файла. Может быть в CP1251 или UTF-8.
MAGIC = b"1CClientBankExchange"


def can_parse(path: str | Path) -> bool:
    """Дешёвая проверка по содержимому: первые 64 байта файла."""
    p = Path(path)
    try:
        with open(p, "rb") as f:
            head = f.read(64)
    except OSError:
        return False
    # BOM игнорируем
    if head.startswith(b"\xef\xbb\xbf"):
        head = head[3:]
    return head.startswith(MAGIC)


def parse(path: str | Path) -> dict:
    raw = _read_text(path)
    return parse_text(raw)


def parse_text(raw: str) -> dict:
    """Главная точка для тестов: принимает уже декодированный текст."""
    lines = _split_lines(raw)
    if not lines or not lines[0].startswith("1CClientBankExchange"):
        raise ValueError("not a 1CClientBankExchange file (missing magic line)")

    header, doc_lines = _split_header_documents(lines[1:])
    our_account = header.get("РасчСчет", "")
    period_from = _parse_date_str(header.get("ДатаНачала"))
    period_to = _parse_date_str(header.get("ДатаКонца"))

    transactions: list[dict] = []
    for doc_block in doc_lines:
        tx = _document_to_tx(doc_block, our_account)
        if tx is not None:
            transactions.append(tx)

    period = None
    if period_from and period_to:
        period = {"from": period_from.isoformat(), "to": period_to.isoformat()}
    elif transactions:
        # фолбэк: если в шапке нет ДатаНачала/ДатаКонца — посчитаем по транзам
        dates = [t["date"] for t in transactions]
        period = {"from": min(dates), "to": max(dates)}

    return {
        "bank": NAME,
        "account": our_account or None,
        "period": period,
        "transactions": transactions,
    }


# ────────────────────────────────────────────────────────────────────────────
# Internals
# ────────────────────────────────────────────────────────────────────────────


class _Tx(TypedDict):
    date: str
    amount: int
    currency: str
    kind: str
    description: str
    raw: str
    id_key: str | None


def _read_text(path: str | Path) -> str:
    """1C пишет CP1251, но в природе встречаются и UTF-8 файлы (когда экспорт
    делает не сама 1С, а какой-нибудь скрипт). Пробуем по очереди."""
    data = Path(path).read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    for enc in ("cp1251", "utf-8", "utf-8-sig"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    # Последний шанс — игнорируем ошибки декодирования
    return data.decode("cp1251", errors="replace")


def _split_lines(text: str) -> list[str]:
    """`\\r\\n` → одна строка; пустые строки выкидываем."""
    out: list[str] = []
    for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        s = ln.strip()
        if s:
            out.append(s)
    return out


def _split_header_documents(lines: list[str]) -> tuple[dict[str, str], list[list[str]]]:
    """Разделяет файл на (header dict, [doc_block, …]).

    `doc_block` — список строк между `СекцияДокумент=…` и `КонецДокумента`,
    включая первую (в ней лежит вид документа: `=Платежное поручение`).
    """
    header: dict[str, str] = {}
    documents: list[list[str]] = []
    current_doc: list[str] | None = None

    for ln in lines:
        if ln == "КонецФайла":
            break
        if ln.startswith("СекцияДокумент"):
            current_doc = [ln]
            continue
        if ln == "КонецДокумента":
            if current_doc is not None:
                documents.append(current_doc)
                current_doc = None
            continue
        if current_doc is not None:
            current_doc.append(ln)
        else:
            # Строка шапки
            if "=" in ln:
                k, _, v = ln.partition("=")
                header[k.strip()] = v.strip()
    # Не закрытый документ — игнорируем (битый файл; 1С тоже бы пожаловалась)
    return header, documents


def _document_to_tx(doc_lines: list[str], our_account: str) -> _Tx | None:
    if not doc_lines:
        return None
    # Первая строка `СекцияДокумент=Платежное поручение`
    first = doc_lines[0]
    _, _, kind_raw = first.partition("=")
    fields: dict[str, str] = {}
    for ln in doc_lines[1:]:
        if "=" not in ln:
            continue
        k, _, v = ln.partition("=")
        fields[k.strip()] = v.strip()

    d = _parse_date_str(fields.get("Дата"))
    if d is None:
        return None
    amount_cents = _parse_amount_cents(fields.get("Сумма"))
    if amount_cents is None:
        return None

    payer_account = fields.get("ПлательщикСчет", "")
    recipient_account = fields.get("ПолучательСчет", "")

    # Определяем направление: чей счёт совпадает с нашим. Если ничего не
    # совпало — например, наш счёт в шапке не указан — по дефолту считаем
    # debit (мы заплатили). Это безопаснее чем «credit» — если ABC посчитает
    # по неверной стороне, доход превратится в расход и пользователь сразу
    # заметит.
    is_credit = bool(our_account) and recipient_account == our_account
    is_debit = bool(our_account) and payer_account == our_account
    if is_credit:
        kind = "Credit"
        signed = abs(amount_cents)
    elif is_debit:
        kind = "Debit"
        signed = -abs(amount_cents)
    else:
        # Никаких подсказок — пусть будет «как написано» (положительное →
        # Credit, отрицательное → Debit). Спека хранит сумму без знака,
        # поэтому это будет Credit.
        if amount_cents < 0:
            kind = "Debit"
            signed = amount_cents
        else:
            kind = "Credit"
            signed = amount_cents

    purpose = fields.get("НазначениеПлатежа", "").strip()

    # Имя контрагента — противоположная сторона.
    if is_debit:
        counterparty_name = fields.get("Получатель1", "") or fields.get("Получатель", "")
        counterparty_inn = fields.get("ПолучательИНН", "")
    elif is_credit:
        counterparty_name = fields.get("Плательщик1", "") or fields.get("Плательщик", "")
        counterparty_inn = fields.get("ПлательщикИНН", "")
    else:
        counterparty_name = fields.get("Получатель1", "") or fields.get("Плательщик1", "") or ""
        counterparty_inn = fields.get("ПолучательИНН", "") or fields.get("ПлательщикИНН", "")

    description_parts: list[str] = []
    if counterparty_name:
        description_parts.append(counterparty_name)
    if purpose:
        description_parts.append(purpose)
    description = " — ".join(description_parts) if description_parts else (kind_raw.strip() or "Документ 1С")

    raw_block = "\n".join(doc_lines)

    # `id_key` берём из ИНН, если есть и валидный (10/12 цифр) — иначе None,
    # тогда analytics упадёт на description.
    id_key: str | None = None
    if counterparty_inn and counterparty_inn.isdigit() and len(counterparty_inn) in (10, 12):
        id_key = counterparty_inn

    return {
        "date": d.isoformat(),
        "amount": signed,
        "currency": "RUB",
        "kind": kind,
        "description": description,
        "raw": raw_block,
        "id_key": id_key,
    }


_DATE_RE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})$")


def _parse_date_str(s: str | None) -> date | None:
    if not s:
        return None
    m = _DATE_RE.match(s.strip())
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def _parse_amount_cents(s: str | None) -> int | None:
    """Принимает `12354.67` (точка), `12354,67` (запятая), `12354` (целое),
    `-100.00`. Возвращает копейки. Знаки + и - сохраняются."""
    if s is None:
        return None
    t = s.strip().replace(" ", "")
    if not t:
        return None
    sign = 1
    if t.startswith("+"):
        t = t[1:]
    elif t.startswith("-"):
        sign = -1
        t = t[1:]
    # точка или запятая — приведём к точке
    t = t.replace(",", ".")
    if "." in t:
        rub_str, _, kop_str = t.partition(".")
    else:
        rub_str, kop_str = t, "00"
    if not rub_str or not rub_str.isdigit():
        return None
    if len(kop_str) == 1:
        kop_str = kop_str + "0"
    if len(kop_str) > 2:
        # больше двух знаков после точки — берём первые два, остальное
        # игнорируем (бывает у банков, выгружающих в EUR/USD с тремя
        # знаками; для рублей 1С такого никогда не пишет)
        kop_str = kop_str[:2]
    if not kop_str.isdigit():
        return None
    return sign * (int(rub_str) * 100 + int(kop_str))
