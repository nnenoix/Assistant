"""verify_claim — defensive layer for the agent's verification protocol.

Re-reads source references (cell addresses, file IDs, message IDs)
RIGHT BEFORE the agent commits to an answer, and reports any mismatch
between what the agent claims and what's actually there. Turns Rules
19-23 from "we hope the model checks" into a forcing function.

Two input forms for `source_refs`:

1. Dict form (verbose, full control):
   `{"kind": "sheets_cell", "spreadsheet_id": "...", "cell": "...", "expected": ...}`

2. Compact string form (preferred for terse calls):
   `"sheets:<spreadsheet_id>:Год факт!B45=3087967"`
   `"named:<spreadsheet_id>:ChistayaPribyl=3087967"`
   `"drive:<file_id>=ОПиУ 2026"`
   `"gmail:<message_id>=invoice"` (expected_subject_contains)
   `"calendar:<event_id>=weekly sync"` (expected_summary_contains)
   Trailing `=<expected>` is optional: bare `"drive:<file_id>"` just checks existence.

The two forms can be mixed in the same `source_refs` list.

Refs are resolved in parallel via ThreadPoolExecutor (Phase 14D) — stress
test showed p50 47s for 50 refs serial; parallel target ~6s.
"""
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

_DEFAULT_MAX_WORKERS = 10
_MAX_ALLOWED_WORKERS = 32


def verify_claim(claim: str, source_refs: list, max_workers: int = _DEFAULT_MAX_WORKERS) -> dict:
    """Re-fetch each `source_ref` and verify it matches what the agent claims.

    Each entry in `source_refs` is either a compact string (see module
    docstring) or a dict with `kind` + relevant fields. Returns
    {verdict, claim, per_ref: [...], discrepancies: [...]}.
    verdict = "ok" | "mismatch" | "error".

    Refs are resolved in parallel via ThreadPoolExecutor. `max_workers` is
    clamped to [1, 32]; default 10. Order of `per_ref` matches input order.
    """
    started = time.perf_counter()
    n = len(source_refs)
    # Clamp workers; no point spinning up more threads than refs.
    workers = max(1, min(max_workers, _MAX_ALLOWED_WORKERS, n or 1))

    # Pre-parse compact strings, recording parse errors at their index.
    # We resolve only the well-formed refs in parallel; parse errors are
    # injected back into per_ref at their original index afterward.
    parsed: list[dict | None] = [None] * n
    parse_errors: dict[int, dict] = {}
    for i, raw_ref in enumerate(source_refs):
        if isinstance(raw_ref, str):
            try:
                parsed[i] = _parse_compact_ref(raw_ref)
            except ValueError as e:
                parse_errors[i] = {"status": "error", "reason": str(e), "raw": raw_ref}
        else:
            parsed[i] = raw_ref

    per_ref: list[dict] = [None] * n  # type: ignore[list-item]
    for i, err in parse_errors.items():
        per_ref[i] = err

    # Resolve valid refs concurrently. `_verify_one` already swallows
    # per-ref exceptions and returns {status: "error", ...}, so we don't
    # need exception handling in the executor — but we add a safety net
    # in case a future _verify_* helper raises.
    if n - len(parse_errors) > 0:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="verify") as pool:
            future_to_idx = {
                pool.submit(_verify_one, parsed[i]): i
                for i in range(n)
                if i not in parse_errors
            }
            for fut in future_to_idx:
                idx = future_to_idx[fut]
                try:
                    per_ref[idx] = fut.result()
                except Exception as e:  # safety net
                    per_ref[idx] = {
                        "status": "error",
                        "reason": f"{type(e).__name__}: {e}",
                    }

    discrepancies = []
    any_error = False
    for i, result in enumerate(per_ref):
        status = result.get("status")
        if status == "mismatch":
            discrepancies.append({
                "ref": parsed[i] if i not in parse_errors else source_refs[i],
                "expected": result.get("expected"),
                "actual": result.get("actual"),
                "reason": result.get("reason"),
            })
        elif status == "error":
            any_error = True

    if discrepancies:
        verdict = "mismatch"
    elif any_error:
        verdict = "error"
    else:
        verdict = "ok"

    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    return {
        "verdict": verdict,
        "claim": claim,
        "per_ref": per_ref,
        "discrepancies": discrepancies,
        "_meta": {
            "ref_count": n,
            "mismatch_count": len(discrepancies),
            "duration_ms": duration_ms,
            "parallel": True,
            "max_workers": workers,
        },
    }


_KIND_TO_DICT_KIND = {
    "sheets": "sheets_cell",
    "named": "named_range",
    "drive": "drive_file",
    "gmail": "gmail_message",
    "calendar": "calendar_event",
}


def _parse_compact_ref(s: str) -> dict:
    """Parse `<kind>:<scope_id>[:<locator>][=<expected>]` → dict form.

    Examples:
      'sheets:SID:Год факт!B45=3087967'
        → {kind:sheets_cell, spreadsheet_id:SID, cell:Год факт!B45, expected:3087967}
      'named:SID:ChistayaPribyl=3087967'
        → {kind:named_range, spreadsheet_id:SID, name:ChistayaPribyl, expected:3087967}
      'drive:FILE_ID=ОПиУ 2026'
        → {kind:drive_file, file_id:FILE_ID, expected_name:ОПиУ 2026}
      'gmail:MSG_ID=invoice'
        → {kind:gmail_message, message_id:MSG_ID, expected_subject_contains:invoice}
      'calendar:EVT_ID=weekly'
        → {kind:calendar_event, event_id:EVT_ID, expected_summary_contains:weekly}
      'drive:FILE_ID' (no `=`) → existence check only.
    """
    # Split off expected after first `=`
    if "=" in s:
        head, expected_str = s.split("=", 1)
        expected: Any = _coerce_expected(expected_str)
    else:
        head = s
        expected = None

    # Split head by `:` — first token is kind, rest depends on kind
    if ":" not in head:
        raise ValueError(f"compact ref missing ':' separator: {s!r}")
    parts = head.split(":", 2)  # at most 3 segments
    kind_short = parts[0]
    dict_kind = _KIND_TO_DICT_KIND.get(kind_short)
    if not dict_kind:
        raise ValueError(f"unknown kind {kind_short!r} in compact ref: {s!r}")

    if dict_kind in {"sheets_cell", "named_range"}:
        # need 3 segments: kind:spreadsheet_id:locator
        if len(parts) < 3:
            raise ValueError(f"{kind_short} ref needs spreadsheet_id and locator: {s!r}")
        ref: dict = {"kind": dict_kind, "spreadsheet_id": parts[1]}
        ref["cell" if dict_kind == "sheets_cell" else "name"] = parts[2]
        if expected is not None:
            ref["expected"] = expected
        return ref

    # drive/gmail/calendar: kind:id  (only 2 segments)
    if len(parts) < 2:
        raise ValueError(f"{kind_short} ref needs an id: {s!r}")
    if dict_kind == "drive_file":
        ref = {"kind": dict_kind, "file_id": parts[1]}
        if expected is not None:
            ref["expected_name"] = str(expected)
        return ref
    if dict_kind == "gmail_message":
        ref = {"kind": dict_kind, "message_id": parts[1]}
        if expected is not None:
            ref["expected_subject_contains"] = str(expected)
        return ref
    if dict_kind == "calendar_event":
        ref = {"kind": dict_kind, "event_id": parts[1]}
        if expected is not None:
            ref["expected_summary_contains"] = str(expected)
        return ref
    raise ValueError(f"unhandled kind {dict_kind!r}")


def _coerce_expected(s: str) -> Any:
    """Try int → float → str coercion for the expected value."""
    s = s.strip()
    # int?
    try:
        return int(s)
    except ValueError:
        pass
    # float?
    try:
        return float(s)
    except ValueError:
        pass
    return s


def _verify_one(ref: dict) -> dict:
    """Resolve a single source_ref. Returns {status, ...}.

    status:
      - "ok" — value matches expected.
      - "mismatch" — actual differs from expected.
      - "error" — couldn't fetch (file missing, scope error, etc.).
      - "skipped" — ref kind not recognized.
    """
    kind = ref.get("kind")
    if kind == "sheets_cell":
        return _verify_sheets_cell(ref)
    if kind == "named_range":
        return _verify_named_range(ref)
    if kind == "drive_file":
        return _verify_drive_file(ref)
    if kind == "gmail_message":
        return _verify_gmail_message(ref)
    if kind == "calendar_event":
        return _verify_calendar_event(ref)
    return {"status": "skipped", "reason": f"unknown ref kind {kind!r}"}


def _verify_sheets_cell(ref: dict) -> dict:
    from src.tools import sheets
    try:
        result = sheets.read_range(
            ref["spreadsheet_id"], ref["cell"],
            formatted=ref.get("formatted", False),
            account=ref.get("account", "main"),
        )
        values = result["values"]
        actual = values[0][0] if values and values[0] else None
        expected = ref.get("expected")
        match = _values_equal(actual, expected)
        return {
            "kind": "sheets_cell",
            "cell": ref["cell"],
            "actual": actual,
            "expected": expected,
            "status": "ok" if match else "mismatch",
            "reason": None if match else "value differs",
            "range_read": result["_meta"].get("range_read"),
        }
    except Exception as e:
        return {
            "kind": "sheets_cell",
            "cell": ref.get("cell"),
            "status": "error",
            "reason": f"{type(e).__name__}: {e}",
        }


def _verify_named_range(ref: dict) -> dict:
    from src.tools import sheets
    try:
        result = sheets.read_named_range(
            ref["spreadsheet_id"], ref["name"],
            account=ref.get("account", "main"),
        )
        values = result["values"]
        actual = values[0][0] if values and values[0] else None
        expected = ref.get("expected")
        match = _values_equal(actual, expected)
        return {
            "kind": "named_range",
            "name": ref["name"],
            "actual": actual,
            "expected": expected,
            "status": "ok" if match else "mismatch",
            "reason": None if match else "value differs",
            "range_read": result["_meta"].get("range_read"),
        }
    except Exception as e:
        return {
            "kind": "named_range",
            "name": ref.get("name"),
            "status": "error",
            "reason": f"{type(e).__name__}: {e}",
        }


def _verify_drive_file(ref: dict) -> dict:
    from src.tools import drive
    try:
        meta = drive.get_metadata(ref["file_id"], account=ref.get("account", "main"))
        expected_name = ref.get("expected_name")
        if expected_name is not None:
            if meta.get("name") == expected_name:
                return {"kind": "drive_file", "file_id": ref["file_id"], "status": "ok",
                        "actual": meta.get("name"), "expected": expected_name}
            return {"kind": "drive_file", "file_id": ref["file_id"], "status": "mismatch",
                    "actual": meta.get("name"), "expected": expected_name,
                    "reason": "name differs"}
        return {"kind": "drive_file", "file_id": ref["file_id"], "status": "ok",
                "actual": meta.get("name")}
    except Exception as e:
        return {"kind": "drive_file", "file_id": ref.get("file_id"),
                "status": "error", "reason": f"{type(e).__name__}: {e}"}


def _verify_gmail_message(ref: dict) -> dict:
    from src.tools import gmail
    try:
        msg = gmail.get_message(ref["message_id"], account=ref.get("account", "main"))
        if expected := ref.get("expected_subject_contains"):
            subj = msg.get("subject") or ""
            if expected in subj:
                return {"kind": "gmail_message", "message_id": ref["message_id"],
                        "status": "ok", "actual": subj, "expected": expected}
            return {"kind": "gmail_message", "message_id": ref["message_id"],
                    "status": "mismatch", "actual": subj, "expected": expected,
                    "reason": "subject mismatch"}
        return {"kind": "gmail_message", "message_id": ref["message_id"], "status": "ok"}
    except Exception as e:
        return {"kind": "gmail_message", "message_id": ref.get("message_id"),
                "status": "error", "reason": f"{type(e).__name__}: {e}"}


def _verify_calendar_event(ref: dict) -> dict:
    from src.tools import calendar
    try:
        ev = calendar.get_event(ref["event_id"], account=ref.get("account", "main"))
        if expected := ref.get("expected_summary_contains"):
            summary = ev.get("summary") or ""
            if expected in summary:
                return {"kind": "calendar_event", "event_id": ref["event_id"],
                        "status": "ok", "actual": summary, "expected": expected}
            return {"kind": "calendar_event", "event_id": ref["event_id"],
                    "status": "mismatch", "actual": summary, "expected": expected,
                    "reason": "summary mismatch"}
        return {"kind": "calendar_event", "event_id": ref["event_id"], "status": "ok"}
    except Exception as e:
        return {"kind": "calendar_event", "event_id": ref.get("event_id"),
                "status": "error", "reason": f"{type(e).__name__}: {e}"}


def _values_equal(a: Any, b: Any) -> bool:
    """Compare two values with reasonable type coercion.
    Treats 3087967 == 3087967.0 == "3087967" as equal."""
    if a == b:
        return True
    # Coerce both to float if possible
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        pass
    # Coerce both to stripped strings
    if str(a).strip() == str(b).strip():
        return True
    return False
