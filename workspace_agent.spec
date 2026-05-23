# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Workspace Agent desktop app.

Build:
    uv run pyinstaller workspace_agent.spec --noconfirm

Result: dist/workspace_agent/workspace_agent.exe + supporting DLLs.

Runtime requirements (NOT bundled — must be present on the machine):
  - `claude` CLI on PATH (claude-agent-sdk shells out to it)
  - Microsoft Edge or Google Chrome (Playwright drives them via msedge/chrome channels)
  - WebView2 runtime (built into Windows 10/11)
  - Internet for Google + WB + Anthropic APIs

Runtime files NEXT TO the exe (created/expected at the install location):
  - client_secret_*.apps.googleusercontent.com.json  — OAuth client (one-time copy)
  - .data/                                            — tokens, chats, uploads, alerts
                                                        (auto-created on first run)
"""

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# Modules with significant lazy-loaded submodules that PyInstaller misses.
hidden_imports = [
    # discovery_cache submodules are NOT needed — every build() call in our
    # code uses cache_discovery=False. Saves ~50MB of bundled JSON.
    *collect_submodules("google_auth_oauthlib"),
    *collect_submodules("pdfminer"),
    *collect_submodules("pdfplumber"),
    *collect_submodules("uvicorn.protocols"),
    *collect_submodules("uvicorn.loops"),
    *collect_submodules("uvicorn.lifespan"),
    "uvicorn.logging",
    "PIL._imaging",
    "PIL.ImageDraw",
    "PIL.ImageFont",
    # claude-agent-sdk shells out to `claude` CLI — no extra imports needed
]

# Data files: static UI + bank parser modules (loaded by importlib) + Apps Script
# default manifests if any.
datas = [
    ("static", "static"),
    ("src/tools/bank_parsers", "src/tools/bank_parsers"),
]

# OAuth client_secret: bundle the dev's Desktop-app client into the exe
# (Google docs are explicit that the client_secret of a Desktop app is
# not actually secret — see https://developers.google.com/identity/protocols/
# oauth2/native-app). End-users sign in with their OWN Google accounts.
# A "this app is not verified" screen appears once unless the user is in
# the OAuth consent screen's Test Users list (up to 100).
import glob as _glob
for _cs in _glob.glob("client_secret_*.apps.googleusercontent.com.json"):
    datas.append((_cs, "."))
    break  # only one expected

# Collect data files from packages that ship data alongside Python code.
# Skip `googleapiclient` — its discovery_cache/ folder is ~50MB of JSON
# documents for every Google API, and we use cache_discovery=False
# everywhere, so we never read those files at runtime.
for pkg in ("google_auth_oauthlib", "pdfplumber",
            "pdfminer", "uvicorn", "fastapi", "starlette"):
    try:
        datas += collect_data_files(pkg)
    except Exception:
        pass

# Exclude obvious bloat we don't actually use
excludes = [
    "tkinter",
    "test", "tests",
    # NB: do not exclude `unittest` — pyparsing's testing module imports it
    # at module-load time, which httplib2 transitively depends on.
    "doctest",
    # Heavy ML stack we have in dev for semantic search; the desktop app
    # works without it (semantic chat search degrades to keyword). Keeping
    # it would balloon the bundle by ~700 MB (torch + transformers).
    "torch", "torchvision", "torchaudio",
    "transformers",
    "sentence_transformers",
    "tensorboard",
    "matplotlib",
    # Notebook / Jupyter
    "IPython", "jupyter", "notebook",
    # Pandas optional plotting / sql
    "pandas.io.formats.style",
    "pandas.plotting._matplotlib",
    # scipy is pulled in transitively but our code doesn't use it — saves ~70MB
    "scipy",
    # We use Playwright with system msedge/chrome channels; bundled Chromium
    # binaries (chromium-1217/) live in %LOCALAPPDATA%\ms-playwright outside
    # the bundle. The Python package wrapper still needed.
]

a = Analysis(
    ["src/desktop.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# NB: do NOT strip discovery_cache/documents/. Even with cache_discovery=False,
# googleapiclient.discovery imports the cache module and consults the
# local documents folder before falling back to network — and the network
# fallback fails in frozen mode (probably SSL cert chain issue). Keeping
# the ~50MB of JSON adds bulk but is required for Drive/Sheets/etc. to work.

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="workspace_agent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,
    upx=False,        # UPX-packing breaks WebView2 in some setups
    console=False,    # No black cmd window — pywebview opens its own
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
    # Windows VersionInfo — Firewall / SmartScreen / Defender prompts
    # show "Workspace Agent" instead of the bare filename. See
    # version_info.txt for the resource contract.
    version="version_info.txt",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=True,
    upx=False,
    upx_exclude=[],
    name="workspace_agent",
)
