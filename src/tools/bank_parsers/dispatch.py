"""Bank dispatcher: pick the right parser for a given PDF.

Phase-1 scope: Alfa only. Subsequent phases register more banks here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol


class _Parser(Protocol):
    NAME: str

    def can_parse(self, path: str | Path) -> bool: ...
    def parse(self, path: str | Path) -> dict: ...


def _registry() -> list[_Parser]:
    from . import (
        alfa,
        clientbank_1c,
        gazprom,
        modul,
        ozon,
        raif,
        sber,
        sber_business,
        tinkoff,
        tochka,
        unicredit,
        vtb,
        wb_bank,
    )

    # Order matters for can_parse fallback; SberBusiness before Sber because
    # SberBusiness has more specific filename heuristics. ClientBank1C идёт
    # первым — `can_parse` у него самый строгий (магическая строка в файле),
    # и .txt-файл точно не должен попасть в банковские парсеры по ошибке.
    # Tochka — после специфических банков, потому что её can_parse работает
    # только по filename, и в редких случаях имя «tochka» может встретиться
    # в названиях файлов других банков.
    return [
        clientbank_1c, alfa, sber_business, sber, gazprom, vtb, raif,
        unicredit, tinkoff, ozon, modul, wb_bank, tochka,
    ]


def parse_statement(pdf_path: str, bank_hint: str | None = None) -> dict:
    """Top-level entrypoint called from combo_actions.run_action."""
    p = Path(pdf_path)
    if not p.exists():
        raise FileNotFoundError(f"pdf not found: {pdf_path}")

    parsers = _registry()
    if bank_hint:
        for parser in parsers:
            if parser.NAME == bank_hint:
                return parser.parse(p)

    for parser in parsers:
        if parser.can_parse(p):
            return parser.parse(p)

    raise ValueError(f"no parser matched: {pdf_path}")
