"""Universal pagination helper.

Most vendor APIs paginate slightly differently:
  - WB:        page param (or cursor in URL)
  - Ozon:      `cursor`/`last_id` returned in response
  - YaMarket:  `paging.nextPageToken` in response
  - SDEK:      `page` param (sequential)
  - МойСклад:  `limit`+`offset` (caller increments)

Rather than reimpl per vendor, this helper drives a generic loop: caller
supplies a function that takes a `cursor_or_offset` arg and returns
`(items, next_cursor_or_None)`. The helper iterates until exhausted, the
max-pages cap is hit, or `max_items` collected. Returns a single
aggregated list + meta about pagination state.
"""
from __future__ import annotations

import time
from typing import Any, Callable


def paginate_all(
    fetch_page: Callable[[Any], tuple[list, Any]],
    initial_cursor: Any = None,
    max_pages: int = 20,
    max_items: int | None = None,
    sleep_ms_between: int = 0,
) -> dict:
    """Iterate `fetch_page(cursor)` until `next_cursor is None` OR we hit
    `max_pages` / `max_items` caps.

    `fetch_page(cursor)` must return `(items_list, next_cursor)`. Pass
    `next_cursor=None` to stop. `initial_cursor` is what's passed on the
    first call (often `None` or `""` or `0`).

    Returns:
      {
        ok: True,
        items: [...combined across pages...],
        pages_fetched: N,
        stopped_reason: "exhausted" | "max_pages" | "max_items" | "error",
        last_cursor: <value>,           # cursor at the time we stopped
        last_error: str | None,
      }
    """
    items: list = []
    cursor = initial_cursor
    pages = 0
    stopped_reason = "exhausted"
    last_error: str | None = None
    while True:
        if pages >= max_pages:
            stopped_reason = "max_pages"
            break
        try:
            page_items, next_cursor = fetch_page(cursor)
        except Exception as e:
            last_error = f"{type(e).__name__}: {str(e)[:200]}"
            stopped_reason = "error"
            break
        pages += 1
        items.extend(page_items)
        if max_items is not None and len(items) >= max_items:
            items = items[:max_items]
            stopped_reason = "max_items"
            break
        if next_cursor is None:
            break
        cursor = next_cursor
        if sleep_ms_between > 0:
            time.sleep(sleep_ms_between / 1000.0)
    return {
        "ok": True,
        "items": items,
        "pages_fetched": pages,
        "stopped_reason": stopped_reason,
        "last_cursor": cursor,
        "last_error": last_error,
        "_meta": {"item_count": len(items)},
    }
