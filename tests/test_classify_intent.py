"""Auto-mode classifier: pure file-lookup → haiku, anything else → sonnet."""
import pytest

from src.agent import classify_intent


@pytest.mark.parametrize("msg", [
    "найди файлы Панина",
    "найди самую свежую таблицу IdealNight",
    "покажи папку Финансы 2029",
    "где лежит ДДС 2026",
    "find all idealnight spreadsheets",
    "list recent files",
    "show files in elena drive",
    "какие файлы есть у Лены?",
    "найди",
])
def test_pure_discovery_picks_haiku(msg):
    assert classify_intent(msg) == "haiku"


@pytest.mark.parametrize("msg", [
    "проанализируй ВБ-отчёт за январь",
    "какая выручка за январь 2026",
    "почему упал finance3_API_v8",
    "напиши Apps Script для агрегации",
    "сравни таблицы Лены и Егора",
    "построй график продаж",
    "посчитай выручку по бренду",
    "fix the SyntaxError in the WB script",
    "write a script that copies rows",
    "calculate revenue per brand",
    "compare these two spreadsheets",
    "найди idealnight и сравни выручку",  # discovery+analysis → sonnet
    "найди файлы Панина и проанализируй структуру",
    "что не так с этой формулой",
])
def test_anything_with_analysis_picks_sonnet(msg):
    assert classify_intent(msg) == "sonnet"


def test_empty_message_defaults_to_sonnet():
    assert classify_intent("") == "sonnet"


def test_long_message_always_sonnet():
    msg = "найди файлы " * 30  # 360+ chars, pure discovery words but long
    assert classify_intent(msg) == "sonnet"


def test_ambiguous_defaults_to_sonnet():
    """If neither bucket clearly matches, default to the smarter model."""
    assert classify_intent("привет") == "sonnet"
    assert classify_intent("ok") == "sonnet"
