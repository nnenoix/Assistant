"""Browser automation for Google APIs that don't have public access.

Currently used for ONE thing: resolving spreadsheet → bound Apps Script ID.
Drive/Apps Script/Drive Activity APIs all refuse to enumerate bound scripts;
Google's web UI is the only authoritative source. We open `script.google.com/
macros/d/<spreadsheet_id>/edit` in a real browser — Google redirects to the
bound script editor (`script.google.com/d/<SCRIPT_ID>/edit`) — and we parse
the final URL.

Persistent profile lives at `.data/browser_profile/`. First call is
non-headless so the user can log in to Google once; subsequent calls reuse
the saved session. After Playwright resolves an ID, the agent caches it in
the bound-script registry, so Playwright fires at most once per spreadsheet.
"""
import re
import time
from pathlib import Path

from src.config import DATA_DIR


BROWSER_PROFILE_DIR = DATA_DIR / "browser_profile"
BROWSER_PROFILE_DIR.mkdir(exist_ok=True)


def _launch_persistent(headless: bool):
    """Lazy import — Playwright loads ~80MB into memory.

    Tries channels in order: Edge (always on Windows) → Chrome (likely on
    user's machine) → bundled Chromium (needs VC++ Redistributable on
    Windows, often missing). The bundled Chromium is only a last resort.
    """
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    common_kwargs = dict(
        user_data_dir=str(BROWSER_PROFILE_DIR),
        headless=headless,
        viewport={"width": 1280, "height": 800},
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-popup-blocking",  # Apps Script opens in a new tab
        ],
    )
    last_err = None
    for channel in ("msedge", "chrome", None):
        try:
            kwargs = dict(common_kwargs)
            if channel:
                kwargs["channel"] = channel
            ctx = pw.chromium.launch_persistent_context(**kwargs)
            return pw, ctx, (channel or "chromium")
        except Exception as e:
            last_err = e
            continue
    pw.stop()
    raise RuntimeError(f"Could not launch any browser (tried msedge, chrome, chromium): {last_err}")


def get_bound_script_id(
    spreadsheet_id: str,
    headless: bool = True,
    timeout_sec: int = 120,
) -> dict:
    """Open the spreadsheet in a real browser, click Extensions → Apps Script,
    capture the script_id from the new tab's URL. Drive/Apps Script APIs don't
    expose bound scripts, so this is the only path. Saves the persistent
    profile so login carries over.

    First call should be `headless=False` if the profile isn't logged in yet
    — a window opens with Google login and waits.

    Returns {script_id, final_url, took_ms, browser_channel}.
    """
    SHEET_URL = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
    t0 = time.time()

    pw, ctx, channel_used = _launch_persistent(headless=headless)
    try:
        page = ctx.new_page()
        page.goto(SHEET_URL, wait_until="domcontentloaded", timeout=timeout_sec * 1000)

        # If we land on login, wait for redirect back (only meaningful when visible)
        deadline = time.time() + timeout_sec
        while "accounts.google.com" in page.url and time.time() < deadline:
            if headless:
                raise RuntimeError(
                    "Not logged in to Google in the Playwright profile. "
                    "Run src.tools.browser.login_interactive() first."
                )
            page.wait_for_timeout(1000)

        if "spreadsheets/d/" not in page.url:
            raise RuntimeError(f"Did not land on spreadsheet (ended at {page.url}). Check sharing.")

        # Wait for spreadsheet UI to be ready (menubar present)
        page.wait_for_selector("#docs-menubars", timeout=30000)

        # The Apps Script editor opens in a NEW tab. Click Extensions → Apps Script.
        # #docs-extensions-menu is the stable DOM id across locales (ru/en/etc.)
        # The submenu has "Apps Script" as plain text (Apps Script(E) with hotkey hint).
        new_pages: list = []
        ctx.on("page", lambda p: new_pages.append(p))

        page.locator("#docs-extensions-menu").click(timeout=5000)
        page.wait_for_timeout(1200)
        # Find the Apps Script item via JS — text-locator misfires (matches the
        # parent menu wrapper). Use role=menuitem + offsetParent visible + text
        # contains "Apps Script". Click by center coordinate, which is stable.
        info = page.evaluate("""() => {
            const all = Array.from(document.querySelectorAll('[role="menuitem"]'));
            const target = all.find(el => el.offsetParent !== null
                && (el.textContent||'').includes('Apps Script')
                && el.id && el.id.startsWith(':'));
            if (!target) return null;
            const r = target.getBoundingClientRect();
            return {x: r.x + r.width/2, y: r.y + r.height/2};
        }""")
        if not info:
            raise RuntimeError("Could not locate 'Apps Script' submenu item")
        page.mouse.click(info["x"], info["y"])

        # Wait for the new page event (fires from ctx.on('page', ...))
        deadline = time.time() + 30
        new_page = None
        while time.time() < deadline:
            for p in new_pages:
                if "script.google.com" in p.url or p.url == "about:blank":
                    # blank may load script URL shortly
                    try:
                        p.wait_for_load_state("domcontentloaded", timeout=2000)
                    except Exception:
                        pass
                    if "script.google.com" in p.url:
                        new_page = p
                        break
            if new_page:
                break
            page.wait_for_timeout(500)

        if not new_page:
            seen = [(p.url, p.is_closed()) for p in (ctx.pages + new_pages)]
            raise RuntimeError(
                f"Apps Script tab didn't open. all pages: {seen}"
            )

        try:
            new_page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass  # URL may already be final
        url = new_page.url

        m = re.search(r"script\.google\.com/(?:u/\d+/)?(?:home/projects|d)/([\w-]{20,})", url)
        if not m:
            raise RuntimeError(f"Could not extract script_id from new tab url: {url}")

        return {
            "script_id": m.group(1),
            "final_url": url,
            "took_ms": int((time.time() - t0) * 1000),
            "browser_channel": channel_used,
        }
    finally:
        try:
            ctx.close()
        except Exception:
            pass
        pw.stop()


def login_interactive(timeout_sec: int = 300) -> dict:
    """Open a visible Chromium window pointing to Google login. The user
    completes login once; profile is saved. Use BEFORE the first call to
    get_bound_script_id, or whenever the saved session expires.
    """
    t0 = time.time()
    pw, ctx = _launch_persistent(headless=False)
    try:
        page = ctx.new_page()
        page.goto("https://accounts.google.com/")
        page.wait_for_timeout(2000)
        # User logs in. We poll for a logged-in indicator (Google's avatar div).
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            url = page.url
            # After login, Google bounces to https://myaccount.google.com/ or similar
            if "myaccount.google.com" in url or "/u/0/" in url:
                return {"logged_in": True, "took_ms": int((time.time() - t0) * 1000)}
            page.wait_for_timeout(1000)
        return {"logged_in": False, "reason": "timeout"}
    finally:
        ctx.close()
        pw.stop()
