"""Pre-reply self-check — lint a draft assistant reply for unattributed
numbers, false-completeness claims, currency without cell address, etc.

Called as a tool (`reply_self_check`) before the agent commits to a reply
that contains specific values derived from tools. The hook runtime can
also call this as a background lint pass.

Detection is heuristic — keyword + regex. No LLM involvement.
"""
from __future__ import annotations

import re


# A "digit cluster" the agent should attribute. We look for 4+ digits
# (with optional thousand-separators: space, NBSP, comma, dot, apostrophe)
# so single years (2026) DON'T trip the lint but a financial sum like
# "3 087 967" or "3,087,967" or "30875.42" does.
_DIGIT_CLUSTER = re.compile(
    r"(?<![A-Za-zА-Яа-я])"           # not preceded by a letter (skip 'B45' style cells)
    r"\d{1,3}(?:[\s ,.’]\d{3}){1,}"  # multi-group thousand-sep form
    r"|"
    r"(?<![A-Za-zА-Яа-я0-9])\d{4,}"            # OR ≥4 raw consecutive digits
)


# Provenance markers — substrings that, near a digit, mean «attributed».
# Cell address: Word!A1 (with optional quotes) or A1-style after a sheet ref.
_CELL_ADDR_RE = re.compile(
    r"['\"`]?[\w\-]+['\"`]?\s*!\s*[A-Z]+\d+(?::[A-Z]+\d+)?"
    r"|[A-Z]+\d+\b"
)
_FILE_ID_RE = re.compile(r"\b[A-Za-z0-9_\-]{20,}\b")  # Drive-style file_id (long opaque string)
_PROVENANCE_HINTS = (
    "file_id=", "message_id=", "event_id=", "thread_id=",
    "spreadsheet_id=", "id=",
    "ячейк", "адрес", "from cell",
)

_COMPLETENESS_CLAIMS = (
    "all files", "all messages", "all events",
    "полный список", "все файлы", "все письма", "все встречи",
    "every file", "every message", "everything", "всё что у тебя есть",
    "complete list", "exhaustive", "polный",
)

# Years 1900-2099 we should NOT flag as financial values
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")

# Currency-token: "3 087 967 ₽" / "1,234.56 руб" / "$5,432"
_CURRENCY_RE = re.compile(
    r"[\d\s ,.]+\s*(?:[₽$€¥£]|руб(?:\.|лей)?|долл|евро|usd|eur|rub|cny)\.?",
    re.IGNORECASE,
)


def self_check(draft_reply: str, *, recent_meta_flags: list[dict] | None = None) -> dict:
    """Lint a draft reply for risk patterns.

    Args:
        draft_reply: the text the agent is about to emit.
        recent_meta_flags: optional list of `_meta` dicts from this turn's
            tool calls — if any have `truncated=true`, completeness claims
            in the reply become more suspicious.

    Returns:
        {ok, warnings: [{kind, span, snippet, suggestion}], _meta}
        `ok=True` if nothing flagged.
    """
    text = draft_reply or ""
    warnings: list[dict] = []
    had_truncated = any(
        (m or {}).get("truncated") for m in (recent_meta_flags or [])
    )

    # ---- Pass 1: unattributed digit clusters ----
    for m in _DIGIT_CLUSTER.finditer(text):
        token = m.group(0)
        # Skip pure years (2026, 2025, etc.)
        if _YEAR_RE.fullmatch(token):
            continue
        start = m.start()
        end = m.end()
        # Provenance check scoped to the SENTENCE — pad-based windows let
        # an earlier sentence's Sheet!B45 cover for an unrelated number.
        # For numbers in markdown tables, _has_provenance expands the scope
        # to the table block + its intro paragraph so one cite above the
        # table covers all rows (see _table_block_window).
        if _has_provenance(text, start):
            continue
        warnings.append({
            "kind": "unattributed_number",
            "span": [start, end],
            "snippet": _trim_snippet(text, start, end),
            "suggestion": (
                "Add cell address (Sheet!A1) or call verify_claim "
                f"with refs like ['sheets:<spreadsheet_id>:Sheet!Cell={token}']."
            ),
        })

    # ---- Pass 2: completeness claims when a recent tool was truncated ----
    if had_truncated:
        lower = text.lower()
        for claim in _COMPLETENESS_CLAIMS:
            idx = lower.find(claim)
            if idx >= 0:
                warnings.append({
                    "kind": "false_completeness_claim",
                    "span": [idx, idx + len(claim)],
                    "snippet": _trim_snippet(text, idx, idx + len(claim)),
                    "suggestion": (
                        "A recent tool result had _meta.truncated=true — "
                        "do NOT claim completeness. Surface the limit instead."
                    ),
                })

    # ---- Pass 3: currency tokens without a cell in the same sentence ----
    for m in _CURRENCY_RE.finditer(text):
        token = m.group(0)
        digits_inside = re.search(r"\d", token)
        if not digits_inside:
            continue
        start = m.start()
        sentence = _sentence_around(text, start)
        if _CELL_ADDR_RE.search(sentence) or _has_provenance(text, start):
            continue
        # Skip if the number portion is a clean year
        bare = re.sub(r"[^\d]", "", token)
        if len(bare) == 4 and 1900 <= int(bare) <= 2099:
            continue
        warnings.append({
            "kind": "currency_without_cell",
            "span": [start, m.end()],
            "snippet": _trim_snippet(text, start, m.end()),
            "suggestion": (
                "Currency value cited without a cell address. Add the source "
                "(e.g. «Чистая прибыль 3 087 967 ₽ (Год факт!B45)»)."
            ),
        })

    # Deduplicate by (kind, span) to avoid double-fire of digit-cluster vs currency
    seen: set[tuple[str, int, int]] = set()
    deduped: list[dict] = []
    for w in warnings:
        key = (w["kind"], w["span"][0], w["span"][1])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(w)

    return {
        "ok": not deduped,
        "warnings": deduped,
        "_meta": {
            "warning_count": len(deduped),
            "had_truncated_source": had_truncated,
            "kinds": sorted({w["kind"] for w in deduped}),
        },
    }


# -------- helpers --------

def _has_provenance(text: str, pos: int) -> bool:
    """Does the surrounding context cite a source?

    Sentence scope first (the original heuristic — keeps unrelated cites
    from leaking across `.?\\n` boundaries). If that misses AND the number
    sits on a markdown table row, retry on the table block + its intro
    paragraph: one cite above the table should cover all its rows."""
    if _provenance_in(_sentence_around(text, pos)):
        return True
    if _is_in_table_row(text, pos):
        return _provenance_in(_table_block_window(text, pos))
    return False


def _provenance_in(ctx: str) -> bool:
    """Provenance marker (cell addr / file_id / hint keyword) in this snippet?"""
    if _CELL_ADDR_RE.search(ctx):
        return True
    if _FILE_ID_RE.search(ctx):
        return True
    low = ctx.lower()
    return any(h in low for h in _PROVENANCE_HINTS)


def _trim_snippet(text: str, start: int, end: int, *, pad: int = 40) -> str:
    s = max(0, start - pad)
    e = min(len(text), end + pad)
    return text[s:e].strip()


def _sentence_around(text: str, pos: int) -> str:
    """Return the sentence containing offset `pos`.

    Delimiters: `.`, `?`, `\\n`. NOT `!` — that would treat 'Sheet!A1' as
    a sentence boundary and cut off the cell address.
    """
    delims = ".?\n"
    starts = [i for i in (text.rfind(c, 0, pos) for c in delims) if i >= 0]
    s = max(starts) + 1 if starts else 0
    ends = [i for i in (text.find(c, pos) for c in delims) if i >= 0]
    e = min(ends) + 1 if ends else len(text)
    return text[s:e].strip()


_FENCE_RE = re.compile(r"(?m)^```")


def _inside_fenced_block(text: str, pos: int) -> bool:
    """True if `pos` falls inside a ``` ... ``` fenced code block.
    Counts opening fences before `pos`; odd count → inside."""
    n = sum(1 for m in _FENCE_RE.finditer(text) if m.start() < pos)
    return n % 2 == 1


def _is_in_table_row(text: str, pos: int) -> bool:
    """`pos` sits on a markdown table row (line starts with `|`) and is
    NOT inside a fenced code block."""
    if _inside_fenced_block(text, pos):
        return False
    line_start = text.rfind("\n", 0, pos) + 1  # 0 if no preceding \n
    line_end = text.find("\n", pos)
    if line_end == -1:
        line_end = len(text)
    return text[line_start:line_end].lstrip().startswith("|")


def _table_block_window(text: str, pos: int) -> str:
    """Window covering the contiguous table block containing `pos`, plus
    its intro paragraph above (up to +8 extra lines, stopping at blank /
    heading / start-of-text). Used to let one cite above a table cover
    all its numeric rows."""
    lines = text.split("\n")
    line_idx = text.count("\n", 0, pos)  # 0-indexed line containing pos
    # Expand down through contiguous `|`-lines.
    end_idx = line_idx
    while end_idx + 1 < len(lines) and lines[end_idx + 1].lstrip().startswith("|"):
        end_idx += 1
    # Expand up through contiguous `|`-lines (covers being mid-table).
    start_idx = line_idx
    while start_idx - 1 >= 0 and lines[start_idx - 1].lstrip().startswith("|"):
        start_idx -= 1
    # Skip the single blank line that conventionally separates intro
    # paragraph from a markdown table. Without this, the walk stops
    # immediately at the separator and never sees the cite.
    if start_idx - 1 >= 0 and not lines[start_idx - 1].strip():
        start_idx -= 1
    # Then walk up through the intro paragraph, capped at +8 lines.
    # Stop at another blank line / heading / start-of-text.
    extra = 0
    while start_idx - 1 >= 0 and extra < 8:
        prev = lines[start_idx - 1]
        if not prev.strip():
            break
        if prev.lstrip().startswith("#"):
            # include the heading, then stop
            start_idx -= 1
            break
        start_idx -= 1
        extra += 1
    return "\n".join(lines[start_idx:end_idx + 1])
