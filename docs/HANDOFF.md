# Project Handoff — Google Workspace Chat Agent

Last updated: 2026-05-22 (Phase 16 marketplace + service-layer expansion: 237 → 403 tools)

## What this project is

Local Python chat agent that drives Google Drive / Sheets / Docs / Slides /
Gmail / Calendar / Apps Script / Forms / Tasks / Contacts via Claude.
FastAPI + pywebview shell, MCP server with 226 tools, multi-Google-account
support, integration tests against the user's real Drive in `CLAUDE-TEST/`.

User: `egor.titt@gmail.com`. Project root: `D:/Google work/`.

## Current state (after Phase 16 — marketplace + service-layer expansion)

- **Tools registered:** 403 (+166 from Phase 16 marketplace/Russian-integrations/infra)
- **Tool categories:** ~72 (auto-derived per prefix)
- **Unit tests:** 566 (green; 1 pre-existing policy test deselected) — +201 from Phase 14+15 (verify parallel, bulk_payload, quota, bulk_tools, apps_script_chunked, phase14_config, read_cache, quota_integration, file_extract, docx_extract, file_analyze, gdoc_extract, gsheet_extract, audio_transcribe, anthropic_client, gdoc_url)
- **Integration tests:** 68 (skipped without `LIVE_GOOGLE_TESTS=1`)
- **System prompt:** 29 rules (added 26-28 from Phase 14 + 29 from Phase 15 file_analyze_ensemble).
- **Runtime safeguards:** `_meta` envelope on every read/search; `error_kind`
  classification at `_wrap_for_sdk` level; META visibility prefix on
  payloads with truncated/empty/default-window flags; background reply
  lint emits `reply_lint` event.
- **Dynamic tool routing:** opt-in via `DYNAMIC_TOOL_ROUTING=1` env. Calls
  `src.tool_router.classify_intent` on first turn, filters SDK session's
  `allowed_tools` to relevant categories.

## Phase history (chronological)

| Phase | Title | Key deliverables |
|---|---|---|
| Round 1 | Bug fixes from real failure | 429 retries (RetryingHttpRequest), copy buttons in UI |
| Round 2 | Anti-stupid-mistakes audit | `_meta` envelope, verification-protocol rules, description tightening |
| 0 | Foundation | scopes for Docs/Slides/Forms/Contacts, integration test scaffolding, CLAUDE-TEST root, seeder |
| 1 | Sheets provenance | named ranges, batch_read, formatted_value, sheet copy |
| 2 | Sheets formatting | charts, pivots, conditional format, validation, freeze |
| 3 | Sheets collab | protected ranges, cell notes |
| 4 | Drive sharing | permissions, revisions, comments, trash |
| 5 | Gmail write-ops | reply, archive, modify labels, threads, filters, batch_modify |
| 6 | Calendar groups | freebusy, find_meeting_slot, recurring (RRULE), overlay |
| 7 | Docs | create, read, append, replace_text, insert_table, export_pdf |
| 8 | Slides | create_from_template, replace_placeholders, add_slide, export_pdf |
| 9 | Forms + Tasks + Contacts | 3 new services (16 tools) |
| 10 | External | web_fetch, web_search, fx_rate (CBR), open_url, pdf_create (reportlab), vision_ocr (Tesseract), translate (Argos) |
| 11 | Helpers | sheets_run_formula, sheets_period_detect, verify_claim, self_run_tests, self_list_tools |
| 12 | Ergonomics + consolidation | error taxonomy, metric_lookup, write_and_verify, compact verify_claim refs, accept names as sheet_ids, drive *_everywhere fold, people→aliases rename, apps_script clasp dedup, description audit |
| 13 | Visibility + discipline | META prefix, reply_self_check + background lint + UI banner, query/metric boundary, tool categories + classify_intent, drive account list form, session-level tool_focus filter |
| 16 | Marketplace + service expansion (2026-05-22) | +166 tools across WB ext / Ozon / Yandex Market / СДЭК+Boxberry+Почта / МойСклад / SMS+Telegram+IMAP / ЮKassa+Тинькофф / Avito+VK / СБИС+Диадок / NLP+DaData+OCR / DuckDB / 1С OData / MDM / approvals / audit / BI / scheduler / skill registry / ZPL / webhooks / locks / tracing / notifications / reports. P0 infra also landed: MCP annotations on all 403, Stripe-style idempotency, RFC 9457 problem+json, retry coverage on requests-based tools, structured truncation envelope, dry_run on destructives (10 native impls). |

## Phase 14 — PENDING (approved, not yet implemented)

**Trigger:** user's production scenario:
- 4 sellers × 5 stores = 20 outlets
- ~5 financial books per outlet → **100 spreadsheets**
- **~7M characters per spreadsheet** (formulas + bound Apps Scripts + tables)
- Total ~700M chars, ~50M cells across all books

**Architectural gaps found in audit (see plan for details):**
1. NO cross-spreadsheet bulk tools (each takes single `spreadsheet_id`)
2. NO internal `asyncio.gather` parallelism — all serial
3. `MAX_TOOL_PAYLOAD = 12 000` silently truncates aggregates
4. NO session-level cache (only process-level `lru_cache` on `_service()`)
5. NO proactive quota budgeter (only reactive `RetryingHttpRequest` on 429)
6. `apps_script_oneshot` is the only "escape hatch" but verbose

**Plan deliverables (8 sub-phases):**

- **14A** `sheets_bulk_metric(spreadsheet_ids, metric, period?)` — parallel `metric_lookup` across N books via `ThreadPoolExecutor(max_workers=10)`
- **14B** `sheets_bulk_read(refs)` — parallel `read_range` across arbitrary `[{spreadsheet_id, range, formatted?}]`
- **14C** `sheets_cross_aggregate(spreadsheet_ids, sheet, cell, op)` — Apps Script-backed server-side aggregator. Generates JS, calls `apps_script_oneshot`. One round-trip
- **14D** Parallelize existing `verify_claim` — stress test showed p50 47s for 50 refs; expected 5-7s after parallelization
- **14E** `SheetReadCache` (TTL+LRU, opt-in via `SHEETS_READ_CACHE=1`)
- **14F** `QuotaBudgeter` (sliding-window, proactive pacing at 50/60 reads/min)
- **14G** System prompt rules 26-28: bulk-first for N≥5 files; Apps Script for 50+; parallel verify
- **14H** Production-scale stress test: 20 books × ~700k chars + 8 acceptance tests (T1-T8)

**Expected wins:**
- "Найди прибыль по 20 магазинам" 80s → 5s (16× via bulk_metric)
- "Суммируй прибыль 100 книг" → 10s (one cross_aggregate round-trip)
- `verify_claim` 50 refs: 47s → ~6s (8× via parallelization)

**Full plan:** `docs/PHASE_14_PLAN.md` (also at `C:\Users\yegor\.claude\plans\rosy-napping-ladybug.md`).

**Phase 14 status — all CODE delivered 2026-05-21; live run pending:**

- ✅ 14D parallelize verify_claim — ThreadPoolExecutor(10), mock 9.9× on 50 refs
- ✅ 14A-prep `_bulk_payload.py` + `_quota.py` — stats, outliers, sliding-window budgeter
- ✅ 14A `sheets_bulk_metric` — mandatory `cell`, compaction, dry_run
- ✅ 14B `sheets_bulk_read` — parallel arbitrary refs
- ✅ 14C-prep persistent Apps Script (`apps_script_src/aggregator/Code.gs`) + `docs/PHASE_14_SETUP.md` + `scripts/verify_phase14_setup.py`
- ✅ 14C `sheets_cross_aggregate` + `cross_aggregate_status` — resumable via CacheService
- ✅ 14C-extra (2026-05-21): chunked-parallel cross_aggregate — fixed Google L7 LB connection-drop retry storm. At N=500 books, naive single-call took 30 min due to 9× retries; chunked (5 × 100 books in parallel) targets ~70-100s. New params: `chunk_size=100`, `max_concurrent=5`.
- ✅ 14E `_read_cache.py` (opt-in via `SHEETS_READ_CACHE=1`) + invalidation in `write_and_verify`
- ✅ 14F `_quota.py` wired into `_wrap_for_sdk` — per-bucket pacing + `_meta.quota_paced_ms`/`quota_remaining_pct`
- ✅ 14G system prompt rules 26-28
- ✅ 14H-build `scripts/build_phase14_fixtures.py` — 3-tier idempotent builder (500 + 50×700k + 1×35M)
- ✅ 14H-stress `scripts/stress_production.py` — T1–T13 acceptance harness

**Next manual steps (USER):**

1. **Apps Script one-time deploy** (5 min) — follow `docs/PHASE_14_SETUP.md` to create the persistent `ChatAgentAggregator` project. Without this, `sheets_cross_aggregate` raises `Phase14ConfigError`. Validate with `uv run python scripts/verify_phase14_setup.py`.
2. **Build fixtures** (~3 hours overnight) — `$env:LIVE_GOOGLE_TESTS = "1"; uv run python scripts/build_phase14_fixtures.py`. Idempotent — partial builds resume on rerun.
3. **Run stress** (~60 min) — `$env:LIVE_GOOGLE_TESTS = "1"; uv run python scripts/stress_production.py`. Writes `summary.json` + `comparison.md` to `.data/sweep_results/phase14_stress_<ts>/`.

---

## Phase 15 status — Multi-LLM File Analysis Ensemble (CODE COMPLETE 2026-05-21)

- ✅ 15A-prep — added `anthropic`/`python-docx`/`openai` deps; `_anthropic_client.py` lazy singleton; `_gdoc_url.py` parser
- ✅ 15B — `file_extract.py` universal dispatcher (TXT/MD/CSV/PDF/XLSX/DOCX/Image/Audio/Google URLs) + `_docx_extract.py`
- ✅ 15C — `file_analyze.py` 3-LLM ensemble (Haiku + Sonnet parallel + Sonnet-judge); saves .md to `.data/analyses/` + notes.add for semantic search; analyses_list/read/search
- ✅ 15D — `_gdoc_extract.py` (via docs.read) + `_gsheet_extract.py` (via sheets.summarize)
- ✅ 15E — `_audio_transcribe.py` via OpenAI Whisper API (graceful skip if OPENAI_API_KEY missing)
- ✅ 15F — registered 5 new tools in registry, system prompt rule 29
- ⏳ 15G+H — smoke + integration tests pending user with API keys

**Auth for Phase 15** — uses `claude_agent_sdk.query()` under the hood with CLI subscription auth. **NO `ANTHROPIC_API_KEY` is used or needed** (project explicit constraint — user does not have one and will not). The sub-LLM calls spawn `claude` CLI subprocesses; each inherits the same OAuth/keychain auth as the main agent.

**Required env vars for Phase 15:**
- `OPENAI_API_KEY` — OPTIONAL only for audio transcription (`.mp3`/`.m4a`/`.wav` files via Whisper API). If missing, `_audio_transcribe` raises with a clear "paste pre-transcribed text instead" hint. All other file types work without it.

**How to test Phase 15 live:**

```powershell
# No special env var setup — uses claude CLI auth that's already in place
cd "D:\Google work"
uv run python -c "
from src.tools import file_analyze
r = file_analyze.analyze(
    path_or_url='D:/path/to/transcript.txt',
    focus='боли клиента + рекомендации',
    save_as='smoke_test'
)
print(r['synthesis'][:500])
print('Saved to:', r['saved_to'])
"
```

Latency: ~30-90s end-to-end (3 claude CLI subprocess calls; ~2-3s cold start each + LLM time). First call may be slower if claude CLI was unloaded.

**Validated live** (2026-05-21 → 2026-05-22):

1. **Single-file smoke** — `file_analyze.analyze(финансовое_консультирование.pdf, focus, save_as)` ran in **58.8s** (parallel A+B = 26.3s, judge = 32.1s), produced 13.5KB structured `.md` synthesis with ₽-tables and stakeholder-split recommendations, indexed in notes with semantic search score 0.51 on "TrueStats цены тарифы" query.
2. **Agent routing** — 4-case behavior test via `claude_agent_sdk.query()` with `can_use_tool` intercept confirmed:
   - "проанализируй PDF" + attachment → agent picks **`file_analyze_ensemble`** with sensible focus + auto `save_as`
   - "найди в PDF упоминание X" + attachment → agent picks **`local_extract_pdf_text`** (correctly skips ensemble)
   - "помнишь анализ TrueStats?" → agent picks **`analyses_search`** first
   - "посмотри файл" (ambiguous) + attachment → agent picks **`local_extract_pdf_text` with max_chars=3000** (peek, per rule 29 case D)

All routing happens via system prompt rule 29 + tool description triggers — no special UI / settings needed from user.

**Production hardening pass (15-polish, completed):**

- 20 edge-case tests added (`tests/test_phase15_edges.py`): empty/whitespace inputs, missing files, unsupported extensions, non-Google URLs, max_chars truncation, pass A/B isolated failures, judge failure cleanly propagated, save_as sanitization (path traversal, special chars, length caps), notes.add graceful degradation, analyses_list/read/search robustness on missing dirs and corrupted .md files
- `file_analyze.analyze()` no longer crashes when notes.add fails — returns synthesis + `_meta.notes_add_failed` flag with the original error string. The `.md` is still on disk regardless.
- Live DOCX test: TrueStats weekly report template (4630 chars) → 69.9s end-to-end. Judge surfaced 3 genuine analytical insights (template misnamed as "financial" but actually operational; bipolar top/weak misses middle class; ROI is a satellite metric, not co-primary with margin). Production-grade.
- Final tally: **580 unit tests green** (+20 from polish), 236 tools registered, 29 system prompt rules.

**Prompt style alignment (15-style, 2026-05-22):** All three prompts (PROMPT_A_SYSTEM / PROMPT_B_SYSTEM / PROMPT_JUDGE_SYSTEM) rewritten to match the actual TrueStats consultation analysis style — prosaic, no tables, no Pass A/B divergence section. Output structure now matches the «Илья Харисов» reference exactly: `## Клиент: [Name]` → `## С чем пришёл клиент` (Главный запрос in direct quote + Боли as paragraphs with 1-3 sentences of context) → `## Что подсветил финансист` (concrete observations with numbers and reasoning) → `## Ключевая формулировка боли` (single meta-quote for marketing) → `## Рекомендации` (prose, not tables). Judge sees 15KB excerpt instead of 5KB. Live validated 2026-05-22: 71.8s wall-clock on 3700-char input section, produced output 1-to-1 match to target style.

---

## Stress test findings (from Phase 13 sweep + interrupted production run)

### Sweep results (15 categories × 8 scenarios each, 120 scenarios)

All 120 PASS. Latency p50=302ms, p95=1520ms globally.

| Category | p50 | p95 | max |
|---|---:|---:|---:|
| reply_check | 0ms | 0ms | 0ms |
| tool_router | 0ms | 0ms | 0ms |
| analytics | 1ms | 8ms | 12ms |
| pdf_gen | 4ms | 5ms | 6ms |
| self (introspect) | 16ms | 54ms | 1679ms |
| fx_rate (CBR) | 310ms | 339ms | 346ms |
| web (fetch+search) | 369ms | 1086ms | 1734ms |
| tasks | 328ms | 344ms | 1255ms |
| calendar | 319ms | 425ms | 639ms |
| drive | 468ms | 520ms | 784ms |
| verify (real reads) | 306ms | 1081ms | 1596ms |
| sheets | 735ms | 989ms | 1486ms |
| slides | 812ms | 964ms | 2224ms |
| gmail | 1026ms | 1285ms | 1680ms |
| docs | 1117ms | 1520ms | 2678ms |

### First stress test (250k rows, 200 files in flood folder)

Completed 8 of 12 stages before crash:
- Giant sheet 250k×8 cols: created in 146s (chunks of 10k)
- 15 server-side QUERY aggregations: **p50 ~7s** regardless of complexity
- Full iter_rows traverse: 250k rows in **57.3s** (51 rounds × ~640ms each)
- Profile + summarize: 17s (server-side formulas on 250k rows)
- Wide sheet 200×5000: 40s
- Drive flood 200 files: **603s (10 min)** — p50 2.2s/create, p95 3.3s, **max 63s** spike (likely 429 retry)
- Batch verify_claim 50 refs × 5 runs: **p50 47s/run** ← **major bottleneck found**
- 200-page PDF: 240ms

**Killer finding:** `verify_claim` parallelization is the highest-leverage Phase 14 item.

### Logs preserved

- `.data/sweep_results/2026-05-20T18-06-21/` — sweep summary + per-tool
- `.data/sweep_results/stress_2026-05-20T18-19-11/` — partial stress run + per_stage.json
- `CLAUDE-TEST/sweep/<timestamps>/` in Drive — actual artifacts (spreadsheets, docs, slides, etc.)
- `CLAUDE-TEST/stress/2026-05-20T18-19-12/` — giant sheet `1jk2Mu92ha01BMzfxU4nnwKqVEqIqowTsfZAJ3AUVULU`, flood folder, wide sheet — **reusable for Phase 14 tests**

---

## Pending follow-ups (post-ultrareview)

### TrueStats weekly-report .docx output — NOT YET BUILT (2026-05-22)

User dropped `TrueStats_Еженедельныи_отчет_шаблон_1.docx` in the project root
(206 KB). It's the **exact format TrueStats produces** for weekly reports.
Requirement: the agent must learn to consume this template and emit reports +
analysis as `.docx` matching it 1:1.

User direction was *"просто запиши тут в память. Нужно чтобы агент умел с этим
работать и выдавать отчеты и анализ, в виде .docx файла."* — **no code change
requested yet**, just captured for the next implementation round.

Open questions to resolve before building:
- Is this a **fill-the-template** flow (placeholders → values from Sheets via
  `docs`/`docx` tooling) or **generate-from-scratch** matching the visual
  style?
- Data source: usually the seller's financial book (the 7M-char spreadsheets
  Phase 14 targets) — confirm with user.
- Existing pieces we'd lean on: `file_extract` (read template),
  `file_analyze_ensemble` (Илья-Харисов prose style — already calibrated
  in Phase 15), no current docx-writer tool — needs new `docs_*` or
  `pdf_gen`-style helper backed by `python-docx`.
- Existing related artifacts in `.data/analyses/`:
  - `truestats_weekly_template.md`
  - `truestats_pdf_v2_truestats_style.md`
  - `.data/test_truestats_format.py`
  (look at these first when picking the build up).

### Auto-update mechanism for distributed builds — NOT YET BUILT (2026-05-22)

User direction: *"нужно будет добавить в будущем функцию обновления
приложению, если оно будет не только у одного юзера"* — captured for the
distribution round, no code change yet.

Trigger: once the `.exe` ships to more than one machine, a manual
"download new zip, replace folder" workflow stops scaling. The app needs a
self-update path.

Sketch for when we pick this up:
- **Manifest endpoint** — e.g. a versioned JSON on a static host (GitHub
  Releases, S3, or our own server) listing `{version, exe_url, sha256,
  release_notes_url}`.
- **Check on startup** (after server is up, before window opens) — compare
  `version` in manifest vs bundled `pyproject.toml` version.
- **In-app notification** — non-blocking banner: «доступна версия X.Y, обновить
  сейчас?» with «позже» / «обновить».
- **Updater** — small sidecar exe that:
  1. Downloads new bundle to `%TEMP%`
  2. Verifies sha256
  3. Waits for parent to exit
  4. Renames `dist/workspace_agent/` → `dist/workspace_agent.old/`
  5. Unpacks new bundle, re-launches the new exe
  6. Cleans up old bundle on success (rollback if new exe doesn't ping
     `/api/accounts` within 30s).
- **Telemetry-free** — no analytics phone-home unless user opts in. Only the
  manifest GET, which is anonymous.
- **Off-by-default in dev** — gate on `sys.frozen` so the dev workflow
  doesn't try to update itself.

Existing prior art to lean on: `PyUpdater`, `pyinstaller-versionfile`, or
roll-our-own with `urllib` + `subprocess` (the latter ~150 LOC, no extra
deps). Recommendation: roll-our-own — the bundle is already 337 MB, no need
to add a heavy framework.

### Reply-lint flood + apps_script_api errors on tabular answers — NOT YET FIXED (2026-05-22)

Live observation: user asked «Лучшие артикулы апрель 2026», agent rendered a
table-form answer with ~30 articles, each row carrying numbers (sales,
margin %, profit). Result on screen:
1. **27× `unattributed_number` warnings** from `reply_self_check` — every
   number in the table got flagged "Add cell address (Sheet!A1) or call
   verify_claim with refs like `['sheets:<spreadsheet_id>:Sheet!Cell:25 473']`"
   even though the numbers come from a single `sheets_query` result the agent
   already cited.
2. **`apps_script_api_run_ad_hoc` returned ошибка** mid-flow (chained after a
   couple of `sheets_query` / `sheets_read_range` / `sheets_run_formula`
   calls). Exact error not captured in screenshot — needs reproduction.

Root cause hypotheses (don't fix yet, just record):
- The reply-lint sees the numbers as bare digits in markdown table cells
  with no surrounding `[ref:...]` token, so each one fires. The fix is
  either:
  - emit a single block-level provenance footer for the whole table
    (one `verify_claim` covers all cells in a query result), OR
  - teach the lint to recognize "this number appears in a column whose
    rows came from a cited `sheets_query` result" and skip those.
- `apps_script_api_run_ad_hoc` errors usually trace to: GCP-project drift
  between `clasp`/Apps-Script-project and Python OAuth (rule 23 in CLAUDE.md
  doc), or missing `script.projects` scope on the active token. Check
  `apps_script_api_status` first when picking this up.

Action when we tackle this:
- Reproduce: run «Лучшие артикулы за апрель 2026» on `main` against the same
  book, capture the actual ad-hoc error payload + the full reply that
  triggered the 27 lint warnings.
- Decide policy: lint should be a *useful signal* about made-up numbers, not
  a noise generator on legitimate aggregate output. If we can't make it
  precise, demote to debug-only or rate-limit (one banner says "27 numbers
  unverified — click to expand" instead of 27 separate items).

---

## How to continue

### Environment

```powershell
cd "D:\Google work"
# Required for live integration tests
$env:LIVE_GOOGLE_TESTS = "1"

# Optional Phase 13 toggles
$env:DYNAMIC_TOOL_ROUTING = "1"   # auto-filter tools per turn
$env:SHEETS_READ_CACHE = "1"      # (when Phase 14E lands) enable TTL cache
```

### Quick health check

```powershell
uv run pytest --deselect tests/test_policy.py::test_missing_file_creates_empty_policy -q
# Expected: 365 passed, 68 skipped, 1 deselected
```

### Re-run sweep

```powershell
$env:LIVE_GOOGLE_TESTS = "1"
uv run python scripts/sweep_tools.py
# Writes to .data/sweep_results/<utc-timestamp>/
```

### Run partial stress test reusing previous giant sheet

```powershell
$env:LIVE_GOOGLE_TESTS = "1"
$env:REUSE_GIANT_SID = "1jk2Mu92ha01BMzfxU4nnwKqVEqIqowTsfZAJ3AUVULU"
uv run python scripts/stress_test.py
```

### Critical files to know

| File | Purpose |
|---|---|
| `src/agent.py` | AgentSession, system prompt (25 rules), run_turn, tool_focus |
| `src/auth.py` | Multi-account OAuth, `RetryingHttpRequest` (5 retries, exp backoff) |
| `src/config.py` | SCOPES list (16 scopes), DATA_DIR, paths |
| `src/tools/registry.py` | All 226 tools registered, `_wrap_for_sdk`, error taxonomy, `_meta_warning_prefix`, `list_categories`, `select_tools` |
| `src/tools/sheets.py` | 33 sheets tools (read/write/format/charts/pivots/protected/notes/named_ranges/metric_lookup/write_and_verify/run_formula/period_detect) |
| `src/tools/drive.py` | 25 drive tools (search/upload/share/permissions/revisions/comments/trash, `account` accepts str OR `"*"` OR list) |
| `src/tools/verify.py` | `verify_claim` — parallel via ThreadPoolExecutor (Phase 14D, 2026-05-21) |
| `src/tools/reply_check.py` | `self_check` — sentence-scoped provenance lint |
| `src/tool_router.py` | `classify_intent` — keyword classifier for tool focus |
| `src/tools/macros.py` | `apps_script_oneshot` — escape hatch for cross-file ops |
| `static/index.html` | UI, including META warning rendering + ReplyLintBanner + ToolFocusBadge |
| `scripts/sweep_tools.py` | 15-category × 8-scenario test harness with timing |
| `scripts/stress_test.py` | 12-stage stress test (250k rows, 200 files), needs Phase 14 expansion |
| `tests/integration/conftest.py` | `claude_test_subfolder` fixture (NEVER auto-cleans) |
| `.data/integration_test_config.json` | Stores CLAUDE-TEST root folder ID |

### Live API requires (manual setup, per user)

- Re-OAuth via `/accounts` UI to grant Docs, Slides, Forms, People scopes if not done.
- Accept TOS in Cloud Console for: docs, slides, forms, people, tasks, calendar-json (Gmail too if write-ops needed).

URLs in [HANDOFF history above](#) — see Phase 0 / Phase 7 / Phase 9 notes.

### Known fragilities

1. ~~**`verify_claim` slow** with many refs — Phase 14D fixes this.~~ ✅ Fixed 2026-05-21: parallel via ThreadPoolExecutor(10). Mock benchmark 9.9× speedup on 50 refs.
2. **Drive creates ~2s/file**, with occasional 63s 429 spikes. Bulk drive operations need pacing.
3. **MAX_TOOL_PAYLOAD = 12k** silently truncates — bulk tools must be payload-aware (Phase 14 design).
4. **Cache stale-read risk** — that's why Phase 14E cache is opt-in.
5. **Console encoding (Windows cp1251)** — `print()` of `→`, `×`, `₽` crashes. Use `logger.py` helper or encode with `errors="replace"`.

### Reply lint catches you forgot

After implementing Phase 14, **the agent's own behavior changes** because system prompt rule 25 will route `reply_self_check` for replies containing numbers. Tests need to verify the agent uses bulk tools for the new scenarios.

---

## Contact / who owns what

Single-developer project. User is the operator AND the primary stakeholder.
All changes go through them. No CI, no team review — just smoke + integration
tests against `egor.titt@gmail.com`'s CLAUDE-TEST folder.
