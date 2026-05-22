"""External-world helpers that don't fit elsewhere.

- `fx_rate`: pull a currency rate vs ₽ from Russia's Central Bank (free, no key).
- `open_url`: open a URL in the user's default browser (handy for "открой эту таблицу в браузере").
"""
import os
import sys
import webbrowser
from datetime import datetime, date

from lxml import etree

from src.tools._retry import retrying_request


_CBR_URL = "https://www.cbr.ru/scripts/XML_daily.asp"


def fx_rate(currency_code: str, date_iso: str | None = None) -> dict:
    """Fetch the official RUB rate for `currency_code` (USD, EUR, CNY, etc.)
    from CBR.ru on `date_iso` (YYYY-MM-DD). If date omitted, today's rate.

    Returns {currency, date, rate_to_rub, nominal, _meta}. `rate_to_rub` is
    how many ₽ for 1 unit of the currency (CBR divides by `nominal` for
    small-denom currencies like KZT).
    """
    if date_iso is None:
        date_iso = date.today().isoformat()
    # CBR format: dd/mm/yyyy
    try:
        d = datetime.strptime(date_iso, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"date_iso must be YYYY-MM-DD, got {date_iso!r}")
    date_req = d.strftime("%d/%m/%Y")
    resp = retrying_request("GET", _CBR_URL, params={"date_req": date_req}, timeout=15)
    resp.raise_for_status()
    # CBR returns windows-1251 — let lxml decode using the XML declaration
    root = etree.fromstring(resp.content)
    target = currency_code.upper()
    for valute in root.findall("Valute"):
        char_code = (valute.findtext("CharCode") or "").upper()
        if char_code == target:
            nominal = int(valute.findtext("Nominal") or "1")
            value_str = (valute.findtext("Value") or "0").replace(",", ".")
            value = float(value_str)
            return {
                "currency": char_code,
                "date": date_iso,
                "rate_to_rub": value / nominal,
                "nominal": nominal,
                "raw_value": value,
                "_meta": {
                    "source": "cbr.ru",
                    "queried_for": date_req,
                },
            }
    return {
        "currency": target,
        "date": date_iso,
        "rate_to_rub": None,
        "_meta": {
            "source": "cbr.ru",
            "queried_for": date_req,
            "error": f"currency {target!r} not found in CBR feed for {date_iso}",
        },
    }


def open_url(url: str) -> dict:
    """Open `url` in the user's default browser.

    Implementation: tries `os.startfile` on Windows for nicer integration
    (uses shell associations), falls back to `webbrowser.open` elsewhere.
    """
    try:
        if sys.platform == "win32":
            os.startfile(url)
        else:
            webbrowser.open(url, new=2)
        return {"ok": True, "url": url, "platform": sys.platform}
    except Exception as e:
        return {"ok": False, "url": url, "error": f"{type(e).__name__}: {e}"}
