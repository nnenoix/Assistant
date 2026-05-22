"""Web fetch + simple web search — pull data from outside Google.

No auth required, no Google APIs. Uses `requests` + `beautifulsoup4`.
Plain HTTP calls go through `_retry.retrying_request` so transient 5xx
/ 429 / connection errors are retried automatically.
"""
import re
from urllib.parse import quote_plus

from bs4 import BeautifulSoup

from src.tools._retry import retrying_request


_MAX_BYTES = 1_000_000  # 1 MB cap
_DEFAULT_TIMEOUT = 15  # seconds
_DEFAULT_UA = "Mozilla/5.0 (compatible; ClaudeWorkAgent/1.0)"


def fetch(
    url: str,
    mode: str = "text",
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict:
    """Fetch a URL. Returns {content, _meta: {status_code, content_type, truncated, url_final}}.

    `mode`:
      - "text" — extract visible text via BeautifulSoup (no scripts/styles).
      - "html" — return raw HTML.
      - "json" — parse as JSON, returns the parsed object as `content`.

    Limit: 1 MB after which truncated=true.
    """
    if mode not in {"text", "html", "json"}:
        raise ValueError(f"unknown mode {mode!r}; allowed: text, html, json")

    headers = {"User-Agent": _DEFAULT_UA}
    resp = retrying_request("GET", url, headers=headers, timeout=timeout, stream=True)
    # Read up to MAX bytes
    raw = b""
    truncated = False
    for chunk in resp.iter_content(chunk_size=65536):
        if not chunk:
            break
        raw += chunk
        if len(raw) >= _MAX_BYTES:
            truncated = True
            break
    resp.close()

    ct = resp.headers.get("Content-Type", "")
    final_url = resp.url
    status = resp.status_code

    if mode == "json":
        import json as _json
        try:
            content = _json.loads(raw.decode("utf-8", errors="replace"))
        except Exception as e:
            return {
                "content": None,
                "_meta": {
                    "status_code": status,
                    "content_type": ct,
                    "url_final": final_url,
                    "error": f"json parse error: {type(e).__name__}: {e}",
                    "truncated": truncated,
                },
            }
    elif mode == "html":
        content = raw.decode("utf-8", errors="replace")
    else:  # text
        html = raw.decode("utf-8", errors="replace")
        soup = BeautifulSoup(html, "lxml")
        # Strip scripts/styles
        for s in soup(["script", "style", "noscript"]):
            s.extract()
        text = soup.get_text(separator="\n")
        # Collapse blank lines
        content = re.sub(r"\n{3,}", "\n\n", text).strip()

    return {
        "content": content,
        "_meta": {
            "status_code": status,
            "content_type": ct,
            "url_final": final_url,
            "bytes_read": len(raw),
            "truncated": truncated,
            "mode": mode,
        },
    }


def search(query: str, max_results: int = 10, timeout: float = _DEFAULT_TIMEOUT) -> dict:
    """Web search via DuckDuckGo HTML (no API key needed).

    Returns {results: [{title, url, snippet}], _meta}. Note: DDG's HTML
    layout changes occasionally; treat as best-effort.
    """
    url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
    headers = {"User-Agent": _DEFAULT_UA}
    resp = retrying_request("POST", url, headers=headers, data={"q": query}, timeout=timeout)
    soup = BeautifulSoup(resp.text, "lxml")
    results = []
    for r in soup.select("div.result")[:max_results]:
        a = r.select_one("a.result__a")
        snippet = r.select_one("a.result__snippet") or r.select_one(".result__snippet")
        if not a:
            continue
        results.append({
            "title": a.get_text(strip=True),
            "url": a.get("href"),
            "snippet": snippet.get_text(strip=True) if snippet else "",
        })
    return {
        "results": results,
        "_meta": {
            "query": query,
            "count": len(results),
            "empty_reason": None if results else "no_matches",
        },
    }
