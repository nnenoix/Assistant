"""Multi-LLM file analysis ensemble (Phase 15C).

3-pass design (per user's approved Phase 15 plan):

  Pass A (Haiku 4.5): extract facts — numbers, names, dates, direct quotes
  Pass B (Sonnet 4.6): interpret — patterns, contradictions, conclusions
  Pass JUDGE (Sonnet 4.6): synthesizes seeing BOTH outputs + 5KB of original text

Pass A and B run in parallel via asyncio.gather; Judge runs serially after.
Total latency: ~max(A, B) + judge = ~25-40s typical for 5-50KB input.

Auth: uses `claude_agent_sdk.query()` underneath — CLI subscription auth, NOT
ANTHROPIC_API_KEY (per project constraint: no API key will ever be set up).
Each sub-LLM call spawns a `claude` CLI subprocess; 3 in parallel via asyncio.

Output saved to .data/analyses/<save_as>.md with YAML front-matter, AND
indexed in notes.json via notes.add(tag=f"analysis:{name}") so the agent
can later semantically search past analyses.
"""
from __future__ import annotations

import asyncio
import datetime
import re
import time as _time
from pathlib import Path
from typing import Any

from src.config import DATA_DIR
from src.tools import _claude_query, file_extract, notes


ANALYSES_DIR = DATA_DIR / "analyses"
ANALYSES_DIR.mkdir(exist_ok=True)

MODEL_FAST = "claude-haiku-4-5"
MODEL_DEEP = "claude-sonnet-4-6"
MODEL_JUDGE = "claude-sonnet-4-6"

# Hard cap on text we feed to LLMs (the wrapper truncates extracted text at
# this length BEFORE sending to Anthropic, so we don't blow context budget).
DEFAULT_MAX_INPUT_CHARS = 100_000

# Three distinct prompt angles. Russian-first because target user (Олья, the
# financial consultant) works with Russian Zoom transcripts. Claude handles
# Russian natively — prompts in Russian get more reliable Russian output.

PROMPT_A_SYSTEM = """Ты извлекаешь ФАКТЫ из транскрипта финансовой консультации TrueStats (селлер на WB/Ozon/Яндекс Маркет + финансист). Никакой интерпретации.

Цель анализа: {focus}

**Что приоритетно искать:**

1. **Профиль клиента:** имя, маркетплейс(ы), категория товара, размер бизнеса (выручка, оборот), штат, давность работы
2. **Главный запрос клиента** — точная прямая цитата того, с чем пришёл («Я хочу понять...», «У меня проблема...», «Мы не можем...»)
3. **Боли клиента (с контекстом):** перечисли все жалобы / проблемы / страхи. Для каждой — 1-2 предложения контекста из текста (цифры, обстоятельства, эмоции)
4. **Конкретные цифры из разговора:**
   - Выручка / план / факт в ₽
   - Маржа % / рентабельность %
   - Комиссии МП, логистика %, реклама/ДРР %, хранение, налоги
   - Оборачиваемость в днях, дни оборота капитала
   - Остатки, неликвиды
   - Расходы операционные ₽/мес
5. **Что финансист сказал / подсветил:** конкретные наблюдения с цифрами («рентабельность 11% слабовата для льготного региона», «логистика 4.5% — норма»)
6. **Прямые цитаты** клиента и финансиста — точно как сказано, в « »

**Правила:**
- Только факты как они есть. Не интерпретируй, не делай выводов.
- Цифры — точно с единицами
- Цитаты — дословно, в « », с указанием кто сказал
- Если файл НЕ консультация — извлекай факты из его домена (имена, даты, цифры, цитаты)
- Не больше 2500 символов. Только русский."""


PROMPT_B_SYSTEM = """Ты интерпретируешь транскрипт финансовой консультации TrueStats. Видишь не только что сказано, но и что СТОИТ ЗА словами.

Цель анализа: {focus}

**Что приоритетно интерпретировать:**

1. **Истинная боль клиента** — что он на самом деле хочет узнать (часто отличается от формального запроса). Например: спрашивает про ДРР, но истинная боль — «не понимаю где утекают деньги»
2. **Системные проблемы клиента** — не одна ошибка, а отсутствие чего-то целого: ДДС нет, точки безубыточности нет, разделения ассортимента нет, связи продажи→прибыль→деньги нет
3. **Где реально теряется маржа** (по структуре): себестоимость / цена / реклама / логистика / хранение / комиссии / неликвиды / оборачиваемость — что из этого критично для ИМЕННО ЭТОГО клиента
4. **Маркетинговые формулировки боли** — то как клиент описывает свою проблему (для последующего использования в постах/рекламе): «оборот есть, прибыли нет», «всё в товар», «не знаю сколько можно вывести», «маржа падает»
5. **Что финансист реально подсветил** — какой именно сдвиг в мышлении клиента он сделал. Это часто не «нашёл катастрофу», а «дал систему принятия решений»
6. **Что осталось за кадром / противоречия:** где клиент недоговаривает, где аргументы финансиста не до конца обоснованы

**Правила:**
- Не пересказывай факты — выявляй смыслы
- Каждый вывод — со ссылкой на конкретное место текста
- Рангируй: что критично для этого селлера, что второстепенно
- Если файл НЕ консультация — интерпретируй в его домене
- Не больше 3000 символов. Только русский."""


PROMPT_JUDGE_SYSTEM = """Ты собираешь финальную сводку по транскрипту финансовой консультации TrueStats. Стиль — прозаический разбор, как пишет аналитик для маркетинга/руководства. **НЕ таблицы, НЕ bullet'ы из одного слова.**

Цель анализа: {focus}

Тебе придут три блока:
1. Pass A — факты (Haiku)
2. Pass B — интерпретация (Sonnet)
3. Excerpt исходного файла (первые 5KB, для верификации)

**Структура отчёта (точная):**

## Клиент: [Имя клиента]
[Маркетплейс(ы)], [категория товара / тип бизнеса в 1 строке]

## С чем пришёл клиент

**Главный запрос:**
 "[прямая цитата клиента дословно, в кавычках]"

**Боли:**

[Каждая боль — заголовок жирным или абзацем, плюс 1-3 предложения контекста под ней. НЕ просто список «реклама / логистика / неликвиды». Развёрнутый абзац объясняет ЧТО конкретно: какие цифры, какие обстоятельства, какие страхи. Пример:

Рост выручки не превращается в рост прибыли
 Клиент говорит, что продают больше, чем в прошлом году, цены повысили, но по прибыли ощутимого роста нет.

Сезонность и провал ожиданий по сезону
 Февраль — ключевой сезон для бритв. Планировали выручку около 7 млн, но сезон прошёл хуже ожиданий. Май–июнь — слабые месяцы, есть страх, что прибыль может приблизиться к нулю.]

## Что подсветил финансист

[Конкретные наблюдения финансиста с цифрами и reasoning. Не «нашёл проблемы», а ИМЕННО какие — со ссылками на цифры из разговора. Пример:

По маркетплейс-метрикам ситуация хорошая
 Финансист прямо говорит, что по структуре расходов "красных флагов" нет: логистика около 4,5% — хороший показатель, реклама около 5–7% — тоже нормально, хранение не критичное.

У клиента хорошая валовая маржинальность — около 20–22%
 Это сильный показатель, особенно с учётом текущих условий на WB.]

## Ключевая формулировка боли

"[Одна мощная цитата (либо реальная клиента, либо синтез на её основе) которая может пойти в маркетинг ТС. 1-3 предложения от первого лица.]"

## Рекомендации финансиста (если были даны)

[Конкретные next steps которые финансист предложил клиенту. Каждая рекомендация — с обоснованием. Без таблиц — прозой.]

**Правила:**
- Только русский. Без воды.
- Цифры точно — суммы в ₽, проценты, дни. Цитируй из excerpt где это возможно.
- НЕ используй таблицы (в этом стиле их нет)
- НЕ пиши секцию «Где Pass A и Pass B расходятся» — это для отладки, не для финального отчёта. Если расхождения есть и они существенны — отрази в основном тексте через формулировку «факты говорят X, но контекст показывает Y».
- НЕ выдумывай — если каких-то данных нет в Pass A/B/excerpt, не пиши их.
- Если файл НЕ консультация (другой тип документа) — адаптируй структуру:
  - "Клиент" → "Документ / Источник"
  - "С чем пришёл клиент" → "О чём документ"
  - "Боли" → "Ключевые проблемы / темы"
  - "Что подсветил финансист" → "Главные наблюдения"
  - "Ключевая формулировка боли" → можно опустить
  - "Рекомендации" → если применимо, иначе опустить"""


PROMPT_JUDGE_USER = """Цель анализа: {focus}

Pass A (факты, модель {model_a}):
---
{analysis_a}
---

Pass B (интерпретация, модель {model_b}):
---
{analysis_b}
---

Excerpt исходного файла (первые 5KB):
---
{original_excerpt}
---

Синтезируй финальную сводку по заданной структуре."""


async def ensemble(text: str, focus: str) -> dict:
    """Run 3-pass ensemble via claude-agent-sdk query() — CLI subscription auth.

    Pass A and B run in parallel via asyncio.gather (each spawns its own
    claude CLI subprocess). Judge runs after, sees both + 5KB of original.

    Returns {synthesis, pass_a, pass_b, _meta}.

    If ONE of A/B fails, the other still gets to the judge with an error
    marker. If both fail, raises RuntimeError.
    """
    if not text.strip():
        raise ValueError("text is empty — nothing to analyze")
    if not focus.strip():
        raise ValueError("focus is empty — describe what to extract")

    overall_started = _time.perf_counter()

    pass_a_coro = _claude_query.call(
        model=MODEL_FAST,
        system_prompt=PROMPT_A_SYSTEM.format(focus=focus),
        user_message=text,
    )
    pass_b_coro = _claude_query.call(
        model=MODEL_DEEP,
        system_prompt=PROMPT_B_SYSTEM.format(focus=focus),
        user_message=text,
    )

    pass_a, pass_b = await asyncio.gather(pass_a_coro, pass_b_coro, return_exceptions=True)
    parallel_ms = round((_time.perf_counter() - overall_started) * 1000, 1)

    if isinstance(pass_a, Exception) and isinstance(pass_b, Exception):
        raise RuntimeError(
            f"Both pass A ({type(pass_a).__name__}) and pass B ({type(pass_b).__name__}) "
            f"failed. A: {pass_a}. B: {pass_b}"
        )

    pass_a_text = (
        pass_a if not isinstance(pass_a, Exception)
        else f"[Pass A failed: {type(pass_a).__name__}: {pass_a}]"
    )
    pass_b_text = (
        pass_b if not isinstance(pass_b, Exception)
        else f"[Pass B failed: {type(pass_b).__name__}: {pass_b}]"
    )

    judge_started = _time.perf_counter()
    judge_user_msg = PROMPT_JUDGE_USER.format(
        focus=focus,
        model_a=MODEL_FAST,
        model_b=MODEL_DEEP,
        analysis_a=pass_a_text,
        analysis_b=pass_b_text,
        # Judge sees a generous excerpt — 15KB lets it verify quotes/numbers
        # from longer transcripts (typical Zoom call: 20-40KB extracted text).
        original_excerpt=text[:15000],
    )
    synthesis = await _claude_query.call(
        model=MODEL_JUDGE,
        system_prompt=PROMPT_JUDGE_SYSTEM.format(focus=focus),
        user_message=judge_user_msg,
    )
    judge_ms = round((_time.perf_counter() - judge_started) * 1000, 1)

    return {
        "synthesis": synthesis,
        "pass_a": pass_a_text,
        "pass_b": pass_b_text,
        "_meta": {
            "model_a": MODEL_FAST,
            "model_b": MODEL_DEEP,
            "judge": MODEL_JUDGE,
            "chars_in": len(text),
            "parallel_ms": parallel_ms,
            "judge_ms": judge_ms,
            "total_ms": round((_time.perf_counter() - overall_started) * 1000, 1),
            "pass_a_failed": isinstance(pass_a, Exception),
            "pass_b_failed": isinstance(pass_b, Exception),
        },
    }


# ============== filename safety ==============

_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_а-яА-ЯёЁ-]+")


def _safe_filename(s: str) -> str:
    """Sanitize string for filename use. Keeps Cyrillic + ASCII alnum + - _."""
    s = s.strip()
    s = _SAFE_NAME_RE.sub("_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s[:80] or "analysis"


def _auto_save_name(source: str) -> str:
    stem = Path(source).stem or "analysis"
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%S")
    return f"{_safe_filename(stem)}_{ts}"


# ============== .md front-matter ==============

def _format_yaml_value(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    # Escape backslash, double-quote, newline, and carriage return so we never
    # emit a value that breaks the YAML front-matter (single-line key: "value").
    s = (
        str(v)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
    return f'"{s}"'


def _front_matter(meta: dict) -> str:
    lines = ["---"]
    for k, v in meta.items():
        lines.append(f"{k}: {_format_yaml_value(v)}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def _parse_front_matter(content: str) -> dict:
    """Minimal parser — covers what _front_matter writes."""
    if not content.startswith("---\n"):
        return {}
    end = content.find("\n---\n", 4)
    if end == -1:
        return {}
    meta: dict = {}
    for line in content[4:end].split("\n"):
        line = line.rstrip()
        if not line or ":" not in line:
            continue
        k, _, v = line.partition(":")
        k = k.strip()
        v = v.strip()
        if v == "null":
            meta[k] = None
        elif v in ("true", "false"):
            meta[k] = v == "true"
        elif v.startswith('"') and v.endswith('"'):
            meta[k] = v[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        else:
            try:
                meta[k] = int(v)
            except ValueError:
                try:
                    meta[k] = float(v)
                except ValueError:
                    meta[k] = v
    return meta


# ============== public tool functions ==============

def analyze(
    path_or_url: str,
    focus: str,
    save_as: str | None = None,
    max_chars: int | None = DEFAULT_MAX_INPUT_CHARS,
) -> dict:
    """End-to-end file analysis: extract → 3-LLM ensemble → save .md + notes.

    Uses claude-agent-sdk query() under the hood (CLI subscription auth, NO
    API key). Each sub-LLM call spawns its own `claude` CLI subprocess.

    Args:
      path_or_url: local file path or Google Doc/Sheet URL
      focus: what to extract (e.g. «боли клиента + рекомендации»)
      save_as: optional name for the .md file; if None auto-generated from source
      max_chars: cap input text before sending to LLMs (default 100k chars)

    Returns:
      {synthesis (markdown), saved_to (path), save_as, notes_id (None if
       notes.add failed), _meta}

    Raises:
      ValueError on empty focus or empty extracted text
      FileNotFoundError if local path missing
      RuntimeError if both LLM passes failed OR judge call failed
    """
    if not focus or not focus.strip():
        raise ValueError("focus is required — describe what to extract/analyze")

    extracted = file_extract.extract_text(path_or_url, max_chars=max_chars)
    text = extracted["text"]
    if not text.strip():
        raise ValueError(f"Extracted text is empty: {path_or_url}")

    result = asyncio.run(ensemble(text, focus))

    save_as_safe = _safe_filename(save_as) if save_as else _auto_save_name(path_or_url)
    md_path = ANALYSES_DIR / f"{save_as_safe}.md"

    meta_front = {
        "source": str(path_or_url),
        "file_kind": extracted["file_kind"],
        "focus": focus,
        "created_at": datetime.datetime.utcnow().isoformat() + "Z",
        "chars_in": result["_meta"]["chars_in"],
        "model_a": result["_meta"]["model_a"],
        "model_b": result["_meta"]["model_b"],
        "judge_model": result["_meta"]["judge"],
        "total_ms": result["_meta"]["total_ms"],
        "extracted_truncated": extracted.get("truncated", False),
    }

    md_content = (
        _front_matter(meta_front)
        + "# Synthesis\n\n"
        + result["synthesis"]
        + f"\n\n---\n\n## Pass A (facts) — {MODEL_FAST}\n\n"
        + result["pass_a"]
        + f"\n\n---\n\n## Pass B (interpretation) — {MODEL_DEEP}\n\n"
        + result["pass_b"]
        + "\n"
    )
    md_path.write_text(md_content, encoding="utf-8")

    # Index in notes for semantic search. First 2KB of synthesis is searchable.
    # If notes.add fails (corrupted notes.json, disk full, etc.) — log and
    # continue. The .md is already on disk and IS the canonical artifact;
    # losing semantic search index is a degradation, not a failure.
    notes_text = (
        f"[Analysis: {Path(path_or_url).name}] Focus: {focus}\n\n"
        f"{result['synthesis'][:2000]}"
    )
    notes_id = None
    notes_error = None
    try:
        notes_entry = notes.add(notes_text, tag=f"analysis:{save_as_safe}")
        notes_id = notes_entry.get("id")
    except Exception as e:
        notes_error = f"{type(e).__name__}: {e}"

    meta_out = {
        **result["_meta"],
        "file_kind": extracted["file_kind"],
        "source": str(path_or_url),
        "md_path": str(md_path),
    }
    if notes_error:
        meta_out["notes_add_failed"] = notes_error

    return {
        "synthesis": result["synthesis"],
        "saved_to": str(md_path),
        "save_as": save_as_safe,
        "notes_id": notes_id,
        "_meta": meta_out,
    }


def list_analyses() -> dict:
    """List saved analyses (newest first). Returns {analyses, _meta}."""
    out: list[dict] = []
    for md_file in sorted(ANALYSES_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            content = md_file.read_text(encoding="utf-8")
            meta = _parse_front_matter(content)
            out.append({
                "name": md_file.stem,
                "path": str(md_file),
                "source": meta.get("source"),
                "focus": meta.get("focus"),
                "created_at": meta.get("created_at"),
                "chars_in": meta.get("chars_in"),
                "file_kind": meta.get("file_kind"),
            })
        except Exception as e:
            from src.tools._errors import _classify_exception
            kind, _ = _classify_exception(e)
            out.append({
                "name": md_file.stem,
                "path": str(md_file),
                "error": str(e)[:200],
                "error_kind": kind,
                "exception_type": type(e).__name__,
            })
    return {"analyses": out, "_meta": {"count": len(out)}}


def read_analysis(name: str) -> dict:
    """Read a saved analysis .md back. name with or without .md extension."""
    name = name.strip()
    if name.endswith(".md"):
        name = name[:-3]
    safe = _safe_filename(name)
    md_path = ANALYSES_DIR / f"{safe}.md"
    if not md_path.exists():
        raise FileNotFoundError(f"Analysis not found: {safe}.md")
    content = md_path.read_text(encoding="utf-8")
    return {
        "name": safe,
        "path": str(md_path),
        "content": content,
        "_meta": _parse_front_matter(content),
    }


def search_analyses(query: str, top_k: int = 5) -> dict:
    """Semantic search across saved analyses via notes.search_semantic, filtered to analysis: tag."""
    # Pull more candidates than requested — filter to analysis: tag — keep top_k
    raw = notes.search_semantic(query, top_k=top_k * 4)
    hits: list[dict] = []
    for item in raw.get("results", []):
        tag = item.get("tag") or ""
        if not tag.startswith("analysis:"):
            continue
        name = tag.removeprefix("analysis:")
        hits.append({
            "name": name,
            "tag": tag,
            "score": item.get("score"),
            "preview": (item.get("text") or "")[:300],
        })
        if len(hits) >= top_k:
            break
    return {
        "results": hits,
        "_meta": {
            "query": query,
            "count": len(hits),
            "search_method": raw.get("_meta", {}).get("search_method"),
        },
    }
