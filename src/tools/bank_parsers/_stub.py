"""Stub parsers for banks where the Rust side is also a stub (NotImplemented).

Once a real parser exists in `crates/bank-parsers/src/banks/{bank}.rs`, the
Python port replaces the stub here.
"""

from __future__ import annotations

from pathlib import Path


def make_stub(name: str):
    class _Stub:
        NAME = name

        @staticmethod
        def can_parse(_path: str | Path) -> bool:
            return False

        @staticmethod
        def parse(_path: str | Path) -> dict:
            raise NotImplementedError(f"{name}Parser::parse not implemented yet")

    _Stub.__name__ = f"{name}Parser"
    return _Stub
