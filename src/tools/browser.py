"""Browser automation for Google APIs that don't have public access.

Used for things Drive/Sheets/Apps Script APIs can't do:
  - Resolving spreadsheet → bound Apps Script ID (`get_bound_script_id`)
  - Switching an Apps Script project's GCP backing (`set_script_gcp_project`)
  - Opening an arbitrary Drive link through a logged-in profile
    (`drive_open` / `drive_list_folder`) — useful when a Drive share-link
    is given but the agent's OAuth accounts don't have direct access.

Persistent profile lives at `.data/browser_profiles/<profile>/`. First
call should be non-headless so the user can log in to Google once;
subsequent calls reuse the saved session. Different profiles can hold
different Google identities (e.g. `default` for egor@..., `elena` for
elena@...).
"""
import re
import time
import urllib.parse
from pathlib import Path

from src.config import DATA_DIR


BROWSER_PROFILES_ROOT = DATA_DIR / "browser_profiles"
BROWSER_PROFILES_ROOT.mkdir(exist_ok=True)
# Backward-compat: old single-profile dir
BROWSER_PROFILE_DIR = DATA_DIR / "browser_profile"
BROWSER_PROFILE_DIR.mkdir(exist_ok=True)


def _profile_dir(profile: str = "default") -> Path:
    """Get the persistent profile directory for `profile` name. 'default'
    points to the legacy .data/browser_profile/ for backward compat."""
    if profile == "default":
        return BROWSER_PROFILE_DIR
    p = BROWSER_PROFILES_ROOT / profile
    p.mkdir(parents=True, exist_ok=True)
    return p


def _launch_persistent(headless: bool, profile: str = "default"):
    """Lazy import — Playwright loads ~80MB into memory.

    Tries channels in order: Edge (always on Windows) → Chrome (likely on
    user's machine) → bundled Chromium (needs VC++ Redistributable on
    Windows, often missing). The bundled Chromium is only a last resort.
    """
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    common_kwargs = dict(
        user_data_dir=str(_profile_dir(profile)),
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
    profile: str = "default",
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

    pw, ctx, channel_used = _launch_persistent(headless=headless, profile=profile)
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


def click_custom_menu(
    spreadsheet_id: str,
    menu_path: list[str],
    headless: bool = True,
    wait_after_click_sec: int = 0,
    timeout_sec: int = 120,
    profile: str = "default",
) -> dict:
    """Open a spreadsheet and click through a custom menu chain. Used to
    trigger bound-script functions that scripts.run can't reach (e.g. when
    the script is in Google's default GCP project, not the caller's).

    `menu_path` is the visible text of each menu item, top-down. The first
    is a top-level menu (e.g. '☰ ВБ'), the rest are submenu items.

    `wait_after_click_sec` keeps the page open after the final click, so the
    bound script (which executes on the server) has time to run. Returns
    {clicked_path, took_ms, browser_channel}.
    """
    SHEET_URL = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
    t0 = time.time()

    pw, ctx, channel_used = _launch_persistent(headless=headless, profile=profile)
    try:
        page = ctx.new_page()
        page.goto(SHEET_URL, wait_until="domcontentloaded", timeout=timeout_sec * 1000)

        deadline = time.time() + timeout_sec
        while "accounts.google.com" in page.url and time.time() < deadline:
            if headless:
                raise RuntimeError("Not logged in — run login_interactive() first.")
            page.wait_for_timeout(1000)
        if "spreadsheets/d/" not in page.url:
            raise RuntimeError(f"Did not land on spreadsheet (ended at {page.url}).")

        page.wait_for_selector("#docs-menubars", timeout=30000)

        # Wait for the FIRST menu_path item to appear (custom menus installed
        # by onOpen take 5-15s on heavy scripts). Poll up to 30s.
        first_label = menu_path[0]
        deadline_menu = time.time() + 30
        while time.time() < deadline_menu:
            found = page.evaluate(
                """(label) => Array.from(document.querySelectorAll('[role="menuitem"], [role="button"]'))
                    .some(el => el.offsetParent !== null && (el.textContent || '').includes(label))""",
                first_label,
            )
            if found:
                break
            page.wait_for_timeout(1000)
        else:
            raise RuntimeError(f"Custom menu {first_label!r} didn't appear within 30s of load")

        for i, label in enumerate(menu_path):
            info = page.evaluate(
                """(label) => {
                    const all = Array.from(document.querySelectorAll('[role="menuitem"], [role="button"]'));
                    const target = all.find(el => el.offsetParent !== null
                        && (el.textContent || '').includes(label));
                    if (!target) return null;
                    const r = target.getBoundingClientRect();
                    return {x: r.x + r.width / 2, y: r.y + r.height / 2, text: target.textContent.trim()};
                }""",
                label,
            )
            if not info:
                # Dump what IS visible for diagnostics
                visible = page.evaluate("""() => Array.from(document.querySelectorAll('[role="menuitem"]'))
                    .filter(el => el.offsetParent !== null)
                    .map(el => (el.textContent||'').trim().substring(0, 40))""")
                raise RuntimeError(f"Step {i+1}: no menu item contains {label!r}. Visible items: {visible[:20]}")
            page.mouse.click(info["x"], info["y"])
            page.wait_for_timeout(800)

        # Keep page alive so server-side execution can run
        if wait_after_click_sec > 0:
            page.wait_for_timeout(wait_after_click_sec * 1000)

        return {
            "clicked_path": menu_path,
            "took_ms": int((time.time() - t0) * 1000),
            "browser_channel": channel_used,
        }
    finally:
        try:
            ctx.close()
        except Exception:
            pass
        pw.stop()


def login_interactive(timeout_sec: int = 300, profile: str = "default") -> dict:
    """Open a visible Chromium window pointing to Google login. The user
    completes login once; profile is saved. Use BEFORE the first call to
    get_bound_script_id, or whenever the saved session expires.
    """
    t0 = time.time()
    pw, ctx, _ = _launch_persistent(headless=False, profile=profile)
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
                return {"logged_in": True, "profile": profile, "took_ms": int((time.time() - t0) * 1000)}
            page.wait_for_timeout(1000)
        return {"logged_in": False, "profile": profile, "reason": "timeout"}
    finally:
        ctx.close()
        pw.stop()


def list_profiles() -> dict:
    """List existing browser profiles. Returns {default: <path>, named: [...]}.
    Each profile is an independent persistent Chromium profile, allowing
    different Google accounts in different sessions.
    """
    named = [p.name for p in BROWSER_PROFILES_ROOT.iterdir() if p.is_dir()] if BROWSER_PROFILES_ROOT.exists() else []
    return {
        "default": str(BROWSER_PROFILE_DIR) if BROWSER_PROFILE_DIR.exists() else None,
        "named": sorted(named),
    }


def set_script_gcp_project(
    script_id: str,
    project_number: str,
    headless: bool = False,
    profile: str = "default",
    timeout_sec: int = 120,
) -> dict:
    """Switch an Apps Script project's associated GCP project to
    `project_number`. Required to make scripts.run work on bound scripts —
    their default (hidden) GCP project blocks API calls from our OAuth client.

    Settings page: script.google.com/d/<id>/edit → ⚙ Project Settings →
    Google Cloud Platform (GCP) Project → Change project → enter number.

    Returns {ok, project_number, took_ms}. Headless False recommended on
    first run so you can confirm the new project is shown after switching.
    """
    SETTINGS_URL = f"https://script.google.com/u/0/home/projects/{script_id}/settings"
    t0 = time.time()
    pw, ctx, channel_used = _launch_persistent(headless=headless, profile=profile)
    try:
        page = ctx.new_page()
        page.goto(SETTINGS_URL, wait_until="domcontentloaded", timeout=timeout_sec * 1000)
        page.wait_for_timeout(3000)

        if "accounts.google.com" in page.url:
            raise RuntimeError("Not logged in — call browser_login_interactive first.")

        # Actual button text on the Apps Script settings page (May 2026):
        # "Изменить тип проекта" (RU) / "Change project" (EN).
        clicked = False
        for label in ("Изменить тип проекта", "Change project", "Изменить проект"):
            try:
                page.get_by_text(label, exact=False).first.click(timeout=3000)
                clicked = True
                break
            except Exception:
                continue
        if not clicked:
            raise RuntimeError("Could not find 'Изменить тип проекта' / 'Change project' button")

        # After click: a text input + "Сохранить"/"Save" button appear inline
        # (no modal, it's an in-place form). Multiple invisible input[type=text]
        # exist on the page (hidden search bars) — find the only VISIBLE +
        # ENABLED one by JS, then click by center coordinate.
        page.wait_for_timeout(1500)
        target = page.evaluate("""() => {
            const inputs = document.querySelectorAll('input[type=text], input:not([type])');
            for (const el of inputs) {
                if (el.offsetParent !== null && !el.disabled && !el.readOnly) {
                    const r = el.getBoundingClientRect();
                    return {x: r.x + r.width/2, y: r.y + r.height/2};
                }
            }
            return null;
        }""")
        if not target:
            raise RuntimeError("No visible+enabled text input found after Change-project click")
        page.mouse.click(target["x"], target["y"])
        page.wait_for_timeout(200)
        page.keyboard.type(str(project_number), delay=20)
        page.wait_for_timeout(500)
        # Click "Сохранить" / "Save"
        confirmed = False
        for label in ("Сохранить", "Save", "Задать проект", "Set project"):
            try:
                page.get_by_text(label, exact=True).first.click(timeout=3000)
                confirmed = True
                break
            except Exception:
                continue
        if not confirmed:
            raise RuntimeError("Could not find 'Сохранить' / 'Save' confirmation button")

        page.wait_for_timeout(4000)
        return {
            "ok": True,
            "script_id": script_id,
            "project_number": project_number,
            "took_ms": int((time.time() - t0) * 1000),
            "browser_channel": channel_used,
        }
    except Exception as e:
        from src.tools._errors import _classify_exception
        kind, status = _classify_exception(e)
        return {
            "ok": False,
            "error": str(e)[:300],
            "exception_type": type(e).__name__,
            "_meta": {"error_kind": kind, "http_status": status},
        }
    finally:
        try:
            ctx.close()
        except Exception:
            pass
        pw.stop()


# ============================================================
# Drive UI fallback — when OAuth accounts can't reach a shared link
# ============================================================
# Use case: someone shared `https://drive.google.com/drive/folders/<ID>`
# with a personal Google account that isn't OAuth-registered with the
# agent. Browser automation through THAT account's logged-in profile is
# the only way the agent can read those files. Tradeoff: slower, uses
# UI which can break on Google A/B changes, harder to verify.


_DRIVE_FOLDER_RE = re.compile(r"/drive/(?:u/\d+/)?folders/([A-Za-z0-9_\-]+)")
_DRIVE_FILE_RE = re.compile(r"/file/(?:u/\d+/)?d/([A-Za-z0-9_\-]+)")
_DOCS_RE = re.compile(r"docs\.google\.com/(document|spreadsheets|presentation|forms)/d/([A-Za-z0-9_\-]+)")


def _parse_drive_url(url: str) -> dict:
    """Extract kind + id from a Drive / Docs / Sheets / Slides URL.

    Returns {kind, id} where kind ∈ {folder, file, document, spreadsheet,
    presentation, forms, unknown} and id is the long alphanumeric token.
    `unknown` means we couldn't recognize the URL shape — the caller can
    still open it but won't get structured folder-listing parsing."""
    m = _DRIVE_FOLDER_RE.search(url)
    if m:
        return {"kind": "folder", "id": m.group(1)}
    m = _DRIVE_FILE_RE.search(url)
    if m:
        return {"kind": "file", "id": m.group(1)}
    m = _DOCS_RE.search(url)
    if m:
        kind_map = {
            "document": "document",
            "spreadsheets": "spreadsheet",
            "presentation": "presentation",
            "forms": "forms",
        }
        return {"kind": kind_map[m.group(1)], "id": m.group(2)}
    return {"kind": "unknown", "id": None}


def drive_open(
    url: str,
    profile: str = "default",
    headless: bool = True,
    timeout_sec: int = 60,
    capture_screenshot: bool = False,
) -> dict:
    """Open ANY Drive / Docs / Sheets / Slides URL through a logged-in
    Playwright profile and report what the page actually shows.

    Why this exists: a Drive share-link is just a string until you
    open it as a human. Drive API obeys ACLs of the OAuth-authenticated
    account; this tool obeys ACLs of whatever Google identity is logged
    in to the `profile`. If a customer / friend shared a folder with
    their elena@... account and you OAuth-registered egor@..., the
    API path returns 404 — this path can reach it because the browser
    profile is `elena@...`-logged-in.

    First-time setup per profile:
        browser_login_interactive(profile="elena")
        # log in with elena@... in the popped window, leave it idle
        # ~10s after redirect to myaccount.google.com — done.

    Returns:
        on success: {
            ok: True,
            parsed: {kind, id},      # what URL pointed at
            resolved_url,            # final URL after redirects
            title,                   # <title> tag content
            access: "granted",
            page_text_preview,       # first 1000 chars of innerText
            screenshot_path?,        # only if capture_screenshot=True
            took_ms, browser_channel
        }
        on access fail: {
            ok: False, error_kind: "permission",
            access: "login_required" | "permission_denied" | "not_found",
            resolved_url, took_ms
        }
    """
    t0 = time.time()
    parsed = _parse_drive_url(url)

    try:
        pw, ctx, channel = _launch_persistent(headless=headless, profile=profile)
    except Exception as e:
        from src.tools._errors import _classify_exception
        kind, _ = _classify_exception(e)
        return {
            "ok": False,
            "error": f"browser launch failed: {str(e)[:200]}",
            "error_kind": kind,
            "_meta": {"profile": profile, "took_ms": int((time.time() - t0) * 1000)},
        }

    try:
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_sec * 1000)
        except Exception as nav_err:
            return {
                "ok": False,
                "error": f"navigation failed: {type(nav_err).__name__}: {str(nav_err)[:200]}",
                "error_kind": "network",
                "_meta": {"resolved_url": page.url, "took_ms": int((time.time() - t0) * 1000)},
            }

        # Give Drive UI a moment to settle — its SPA needs ~1-2s to populate.
        page.wait_for_timeout(2000)
        final_url = page.url

        # Login redirect detection
        if "accounts.google.com" in final_url or "ServiceLogin" in final_url:
            return {
                "ok": False,
                "error": "redirected to Google login — profile not authenticated",
                "error_kind": "auth_scope",
                "access": "login_required",
                "fix_hint": (
                    f"call browser_login_interactive(profile={profile!r}) "
                    "once in non-headless mode"
                ),
                "_meta": {
                    "resolved_url": final_url,
                    "profile": profile,
                    "took_ms": int((time.time() - t0) * 1000),
                },
            }

        # No-access detection: Drive renders a "Request access" / "You need
        # access" interstitial. Title typically becomes "Доступ к ...".
        title = (page.title() or "")[:300]
        page_text = ""
        try:
            page_text = page.evaluate("() => document.body.innerText || ''")[:2000]
        except Exception:
            pass
        if any(needle in page_text for needle in (
            "You need access", "Request access",
            "Запросить доступ", "Нужен доступ",
            "Нет доступа", "доступа нет",
        )):
            return {
                "ok": False,
                "error": "page shows no-access interstitial",
                "error_kind": "permission",
                "access": "permission_denied",
                "title": title,
                "_meta": {
                    "resolved_url": final_url,
                    "profile": profile,
                    "page_text_preview": page_text[:500],
                    "took_ms": int((time.time() - t0) * 1000),
                },
            }

        # 404-ish: "Sorry, the file you have requested does not exist"
        if any(needle in page_text for needle in (
            "does not exist", "не существует", "удалён", "deleted",
        )) and parsed["kind"] != "unknown":
            # Heuristic — could false-positive on a doc literally titled
            # "things that deleted...". Caller can sanity-check.
            return {
                "ok": False,
                "error": "page reports the resource doesn't exist",
                "error_kind": "not_found",
                "access": "not_found",
                "title": title,
                "_meta": {
                    "resolved_url": final_url,
                    "page_text_preview": page_text[:500],
                    "took_ms": int((time.time() - t0) * 1000),
                },
            }

        result: dict = {
            "ok": True,
            "parsed": parsed,
            "resolved_url": final_url,
            "title": title,
            "access": "granted",
            "page_text_preview": page_text[:1000],
            "_meta": {
                "profile": profile,
                "browser_channel": channel,
                "took_ms": int((time.time() - t0) * 1000),
            },
        }

        if capture_screenshot:
            shot_dir = DATA_DIR / "browser_screenshots"
            shot_dir.mkdir(parents=True, exist_ok=True)
            shot_path = shot_dir / f"drive_{int(time.time())}.png"
            try:
                page.screenshot(path=str(shot_path), full_page=False)
                result["screenshot_path"] = str(shot_path)
            except Exception:
                pass

        return result
    finally:
        try:
            ctx.close()
        except Exception:
            pass
        pw.stop()


def drive_list_folder(
    url: str,
    profile: str = "default",
    headless: bool = True,
    timeout_sec: int = 60,
    max_items: int = 200,
) -> dict:
    """Open a Drive folder URL and parse the list of child items from the UI.

    Drive folder UI renders each child as a `<div data-id="<FILE_ID>">`
    container with the title in `<div aria-label="<NAME>">`. We pull
    these via `page.evaluate(...)` and return a structured list.

    Returns:
        on success: {ok: True, parsed, items: [{id, name, kind?}], ...}
        on access fail: same shape as `drive_open`.

    `kind` for each item is inferred from a CSS class hint Drive's UI
    sets (`-folder`, `-document`, `-spreadsheet`, ...). May be `None`
    if the hint isn't found — caller can re-open the item URL to find out.
    """
    parsed = _parse_drive_url(url)
    if parsed["kind"] != "folder":
        return {
            "ok": False,
            "error": f"URL is not a folder ({parsed['kind']}); use drive_open instead",
            "error_kind": "bad_input",
            "parsed": parsed,
        }

    t0 = time.time()
    try:
        pw, ctx, channel = _launch_persistent(headless=headless, profile=profile)
    except Exception as e:
        from src.tools._errors import _classify_exception
        kind, _ = _classify_exception(e)
        return {
            "ok": False, "error": f"browser launch: {str(e)[:200]}",
            "error_kind": kind, "_meta": {"profile": profile},
        }

    try:
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_sec * 1000)
        page.wait_for_timeout(3000)  # SPA settle

        if "accounts.google.com" in page.url:
            return {
                "ok": False, "error": "login required", "error_kind": "auth_scope",
                "access": "login_required",
                "fix_hint": f"browser_login_interactive(profile={profile!r})",
                "_meta": {"profile": profile, "resolved_url": page.url},
            }

        # Wait for the file-list grid to render. Drive lazy-loads items —
        # we scroll the container until count stops growing or max hit.
        # Selector for grid item: `[data-id]` is stable.
        try:
            page.wait_for_selector("[data-id]", timeout=10000)
        except Exception:
            # No items selector ever appeared — could be empty folder OR
            # access denied without the friendly interstitial. Check
            # page text to decide.
            text = page.evaluate("() => document.body.innerText || ''")[:500]
            if any(n in text for n in ("Request access", "Запросить доступ", "Нужен доступ")):
                return {
                    "ok": False, "error": "permission denied", "error_kind": "permission",
                    "access": "permission_denied",
                    "_meta": {"resolved_url": page.url, "page_text_preview": text[:300]},
                }
            return {
                "ok": True, "parsed": parsed, "items": [],
                "resolved_url": page.url,
                "_meta": {"reason": "no items selector appeared (empty folder?)",
                          "profile": profile, "took_ms": int((time.time() - t0) * 1000)},
            }

        # Light scroll loop to surface lazy-loaded children.
        last_count = -1
        for _ in range(8):  # cap iterations
            items = page.evaluate("() => document.querySelectorAll('[data-id]').length")
            if items == last_count or items >= max_items:
                break
            last_count = items
            page.mouse.wheel(0, 2000)
            page.wait_for_timeout(700)

        # Extract id + name + kind hint from the rendered nodes.
        raw_items = page.evaluate("""(max) => {
            const out = [];
            const seen = new Set();
            for (const el of document.querySelectorAll('[data-id]')) {
                if (out.length >= max) break;
                const id = el.getAttribute('data-id');
                if (!id || seen.has(id)) continue;
                seen.add(id);
                // Name candidates — Drive shifts these around between
                // grid view and list view. Try a few selectors.
                let name = '';
                const ariaEl = el.querySelector('[aria-label]');
                if (ariaEl) name = ariaEl.getAttribute('aria-label') || '';
                if (!name) name = (el.innerText || '').split('\\n')[0].trim();
                // Kind hint via class — `a-s-fa-Ha-pa` etc. obfuscated.
                // Look for `[role=img]` aria-label which is the file-type icon.
                let kind = null;
                const img = el.querySelector('[role="img"][aria-label]');
                if (img) {
                    const lbl = (img.getAttribute('aria-label') || '').toLowerCase();
                    if (lbl.includes('folder') || lbl.includes('папка')) kind = 'folder';
                    else if (lbl.includes('sheet') || lbl.includes('таблиц')) kind = 'spreadsheet';
                    else if (lbl.includes('doc') || lbl.includes('документ')) kind = 'document';
                    else if (lbl.includes('slide') || lbl.includes('презентац')) kind = 'presentation';
                    else if (lbl.includes('pdf')) kind = 'pdf';
                    else if (lbl.includes('image') || lbl.includes('изображ')) kind = 'image';
                }
                out.push({id, name, kind});
            }
            return out;
        }""", max_items)

        return {
            "ok": True,
            "parsed": parsed,
            "resolved_url": page.url,
            "items": raw_items,
            "_meta": {
                "profile": profile,
                "browser_channel": channel,
                "count": len(raw_items),
                "truncated": len(raw_items) >= max_items,
                "took_ms": int((time.time() - t0) * 1000),
            },
        }
    except Exception as e:
        from src.tools._errors import _classify_exception
        kind, status = _classify_exception(e)
        return {
            "ok": False, "error": str(e)[:300],
            "exception_type": type(e).__name__,
            "_meta": {"error_kind": kind, "http_status": status,
                      "took_ms": int((time.time() - t0) * 1000)},
        }
    finally:
        try:
            ctx.close()
        except Exception:
            pass
        pw.stop()
