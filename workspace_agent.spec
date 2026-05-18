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
    *collect_submodules("googleapiclient.discovery_cache"),
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
    # OAuth client_secret is NOT bundled (would expose creds to anyone with
    # the exe). User copies their own client_secret_*.json next to the exe.
]

# Collect data files from packages that ship data alongside Python code
for pkg in ("googleapiclient", "google_auth_oauthlib", "pdfplumber",
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

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="workspace_agent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,        # UPX-packing breaks WebView2 in some setups
    console=False,    # No black cmd window — pywebview opens its own
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="workspace_agent",
)
