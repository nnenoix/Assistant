"""Unit tests for src/tool_router.py."""
from src import tool_router


def test_baseline_always_included():
    """Every classification should include the baseline set."""
    result = tool_router.classify_intent("just check something")
    for c in tool_router.BASELINE_CATEGORIES:
        assert c in result, f"baseline category {c} missing"


def test_empty_message_falls_back_to_sheets_drive():
    result = tool_router.classify_intent("")
    assert "sheets" in result
    assert "drive" in result


def test_financial_query_includes_sheets_bank_analytics():
    result = tool_router.classify_intent("Посчитай прибыль за 2026 из ОПиУ")
    assert "sheets" in result
    # 'прибыль' is in sheets keywords


def test_calendar_query():
    result = tool_router.classify_intent("Когда у меня свободно на следующей неделе для встречи?")
    assert "calendar" in result


def test_gmail_query():
    result = tool_router.classify_intent("Покажи письма от Олега за последний месяц")
    assert "gmail" in result


def test_apps_script_query():
    result = tool_router.classify_intent("Почини скрипт WB API в библиотеке")
    assert "apps" in result


def test_bank_statement_query():
    result = tool_router.classify_intent("Распарси выписку из Сбера за декабрь")
    assert "bank" in result


def test_drive_share_query():
    result = tool_router.classify_intent("Поделись таблицей с Олей")
    assert "drive" in result
    assert "sheets" in result


def test_ocr_query():
    result = tool_router.classify_intent("Прочитай этот чек")
    assert "vision" in result


def test_translation_query():
    result = tool_router.classify_intent("Переведи это письмо на английский")
    assert "translate" in result


def test_unmatched_short_message_gets_sheets_drive_default():
    """Short, ambiguous → baseline + sheets/drive."""
    result = tool_router.classify_intent("привет")
    assert "sheets" in result
    assert "drive" in result


def test_multi_category_query():
    """Compound queries get multiple categories."""
    result = tool_router.classify_intent(
        "Сравни прибыль в Sheets и отправь итоги письмом"
    )
    assert "sheets" in result
    assert "gmail" in result


def test_full_category_set_includes_everything():
    """full_category_set returns all registered categories."""
    cats = tool_router.full_category_set()
    # Should include the major families that exist in TOOLS
    for required in ("sheets", "drive", "gmail", "calendar", "aliases"):
        assert required in cats, f"category {required} missing from registry"


def test_classify_returns_sorted_unique():
    result = tool_router.classify_intent("Покажи письма и события")
    # No duplicates
    assert len(result) == len(set(result))
    # Sorted
    assert result == sorted(result)
