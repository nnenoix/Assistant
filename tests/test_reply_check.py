"""Unit tests for src/tools/reply_check.py."""
from src.tools import reply_check


def test_clean_reply_no_warnings():
    result = reply_check.self_check("Готово, всё сделал.")
    assert result["ok"] is True
    assert result["warnings"] == []


def test_year_not_flagged():
    """2026 is a year — should NOT trip the unattributed-number lint."""
    result = reply_check.self_check("Отчёт за 2026 год собран.")
    assert result["ok"] is True


def test_unattributed_currency_flagged():
    """3 087 967 ₽ without (Sheet!Cell) → warning."""
    result = reply_check.self_check("Чистая прибыль 3 087 967 ₽ за 2026 год.")
    assert result["ok"] is False
    kinds = {w["kind"] for w in result["warnings"]}
    assert "currency_without_cell" in kinds or "unattributed_number" in kinds


def test_attributed_currency_passes():
    """Currency cited with Sheet!Cell → no warning."""
    result = reply_check.self_check("Чистая прибыль 3 087 967 ₽ (Год факт!B45).")
    assert result["ok"] is True


def test_attributed_with_file_id_passes():
    """Long file_id-like opaque string near a number → counts as provenance."""
    result = reply_check.self_check(
        "Found 12500 entries in file_id=1SDP2t7iHa95Ss0MYork6hDntf2N0sFWf."
    )
    assert result["ok"] is True


def test_attributed_with_explicit_hint_passes():
    result = reply_check.self_check("Profit: 3087967 (ячейка Год факт!B45)")
    assert result["ok"] is True


def test_unattributed_4digit_number_flagged():
    """5+ digit standalone with NO provenance → flagged."""
    result = reply_check.self_check("Найдено 12500 строк.")
    assert result["ok"] is False
    assert any(w["kind"] == "unattributed_number" for w in result["warnings"])


def test_3digit_not_flagged():
    """A 3-digit number alone shouldn't fire — too noisy."""
    result = reply_check.self_check("Нашёл 150 строк.")
    assert result["ok"] is True


def test_completeness_claim_with_truncated_source():
    """Claiming 'all files' when a recent _meta.truncated=True → flagged."""
    result = reply_check.self_check(
        "Вот полный список файлов в Drive.",
        recent_meta_flags=[{"truncated": True, "returned_count": 200}],
    )
    kinds = {w["kind"] for w in result["warnings"]}
    assert "false_completeness_claim" in kinds


def test_completeness_claim_without_truncation_passes():
    """Same claim but no truncated _meta → not flagged."""
    result = reply_check.self_check(
        "Вот полный список файлов в Drive.",
        recent_meta_flags=[{"truncated": False}],
    )
    kinds = {w["kind"] for w in result["warnings"]}
    assert "false_completeness_claim" not in kinds


def test_meta_block_kinds():
    """Result _meta should aggregate kinds for the summary."""
    result = reply_check.self_check(
        "Прибыль 3087967 за весь период. Все файлы найдены.",
        recent_meta_flags=[{"truncated": True}],
    )
    assert "kinds" in result["_meta"]
    assert result["_meta"]["warning_count"] >= 1
    assert result["_meta"]["had_truncated_source"] is True


def test_dedup_currency_and_digit_at_same_span():
    """When currency_without_cell and unattributed_number could both fire at the
    same span, we deduplicate by (kind, span)."""
    result = reply_check.self_check("3 087 967 ₽")
    # The currency lint and digit lint may both flag — but spans should be distinct
    seen_spans = [(w["kind"], tuple(w["span"])) for w in result["warnings"]]
    assert len(seen_spans) == len(set(seen_spans))


def test_long_text_with_mix():
    """Mixed reply: some attributed, some not."""
    reply = (
        "Чистая прибыль: 3 087 967 ₽ (Год факт!B45). "
        "Общий объём операций: 12500 транзакций. "
        "Это все файлы которые я нашёл."
    )
    result = reply_check.self_check(
        reply, recent_meta_flags=[{"truncated": True}],
    )
    # 12500 unattributed → flagged
    # "все файлы" + truncated → flagged
    assert result["ok"] is False
    kinds = {w["kind"] for w in result["warnings"]}
    assert "unattributed_number" in kinds
    assert "false_completeness_claim" in kinds


# ---------- table-aware provenance (the "27 warning flood" fix) ----------

def test_cited_table_no_flag():
    """One cite above a markdown table covers every number in its rows."""
    reply = (
        "Топ артикулов за апрель 2026. Источник: Год факт!B2:D31.\n"
        "\n"
        "| Артикул | Выручка | Маржа % |\n"
        "|---|---|---|\n"
        "| Замок 116мм | 125000 | 18.5 |\n"
        "| Шланг 5м | 87500 | 22.1 |\n"
        "| Сумка коричневая | 64200 | 15.3 |\n"
        "| Шпингалет хром | 41800 | 12.7 |\n"
        "| Реле напряжения | 39600 | 19.9 |\n"
    )
    result = reply_check.self_check(reply)
    assert result["ok"] is True, [w["kind"] for w in result["warnings"]]


def test_uncited_table_still_flags():
    """Same table, no preamble cite — numbers must still get flagged."""
    reply = (
        "Вот результаты:\n"
        "\n"
        "| Артикул | Выручка | Маржа % |\n"
        "|---|---|---|\n"
        "| Замок 116мм | 125000 | 18.5 |\n"
        "| Шланг 5м | 87500 | 22.1 |\n"
    )
    result = reply_check.self_check(reply)
    assert any(w["kind"] == "unattributed_number" for w in result["warnings"])


def test_distant_cite_does_not_leak_into_table():
    """A cite paragraph far above the table (separated by blank lines and
    unrelated prose) must NOT cover the table — the upward walk stops at
    the first blank line."""
    reply = (
        "Чистая прибыль: 3 087 967 ₽ (Год факт!B45).\n"
        "\n"
        "Это была сводка по году. Теперь данные по складам:\n"
        "Раздел про региональные остатки, без привязки к источнику.\n"
        "\n"
        "| Склад | Остаток |\n"
        "|---|---|\n"
        "| Москва | 45200 |\n"
        "| Питер | 31800 |\n"
    )
    result = reply_check.self_check(reply)
    # Table numbers (45200, 31800) must be flagged; cited 3 087 967 stays clean.
    flagged_snippets = [w["snippet"] for w in result["warnings"] if w["kind"] == "unattributed_number"]
    assert any("45200" in s or "31800" in s for s in flagged_snippets), flagged_snippets


def test_pipe_lines_in_code_fence_not_treated_as_table():
    """`|`-separated lines inside a ```python fence are code, not a table.
    Don't grant them table-mode provenance."""
    reply = (
        "Вот пример:\n"
        "\n"
        "```python\n"
        "data = [\n"
        "    | 'A' | 125000 |\n"
        "    | 'B' |  87500 |\n"
        "]\n"
        "```\n"
    )
    result = reply_check.self_check(reply)
    assert any(w["kind"] == "unattributed_number" for w in result["warnings"]), (
        result["warnings"]
    )
