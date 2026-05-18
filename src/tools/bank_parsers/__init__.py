"""Bank-statement PDF parsers (Python port).

Each module exposes:
    NAME: str           — bank id (matches Rust core::Bank)
    can_parse(path) -> bool
    parse(path) -> dict — Statement (matches combo-core::Statement JSON shape)
"""

from __future__ import annotations
