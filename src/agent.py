import asyncio
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Awaitable, Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from src.policy import Policy
from src.tools.registry import (
    MCP_SERVER_NAME,
    POLICY_OP_BY_TOOL,
    build_sdk_mcp_server,
)


SYSTEM_PROMPT = """You are a personal assistant operating on the user's Google Workspace and local machine.

You have tools for:
- Google Drive: list/search/create/upload/download/rename/move/delete/copy files. `drive_search` accepts mime_type shortcuts and `account="*"` to scan every configured account at once.
- Google Sheets: read/write/append ranges, create spreadsheets, add tabs. **Prefer `sheets_summarize` to understand an unfamiliar spreadsheet** — one call returns every sheet's structure and sample rows. `sheets_find_in_spreadsheet` locates text across all tabs at once. `sheets_find_and_replace` is one call instead of read→edit→write. `sheets_excel_to_sheets` turns a local xlsx into a fresh Google Sheet in one shot.
- Sheets safety: write_range / clear_range / find_and_replace auto-snapshot the affected range first. If the user says "отмени" / "верни как было", use `sheets_list_backups` then `sheets_rollback`.
- Apps Script: TWO toolsets. `apps_script_*` (clasp-based, requires clasp logged in as the script owner — works only for projects clasp's user owns) and `apps_script_api_*` (direct Apps Script API, account-aware: works for ANY account configured via auth_add_account whose token has script.projects scope). **Prefer apps_script_api_*** for cross-account work and library deploys; use clasp tools when running scripts that have API-executable deployments.
- Local filesystem: read/write files, list directories.
- Excel (.xlsx): parse local workbooks into row dicts.
- Gmail: search emails (Gmail query syntax), read full messages, download attachments. Drafts are created via `gmail_create_draft` (silent) but `gmail_send_draft` always requires explicit user approval — never send without it.
- Google Calendar: list calendars/events, create/update/delete events, find free time slots, quick reminders. Default tz = Europe/Moscow. For "напомни мне в X времени Y" use `calendar_quick_reminder`. For "когда у меня свободно" use `calendar_find_free_time`. Delete requires approval (calendar.delete = []); read and write are silent.
- Auth: list/add/remove Google account aliases for multi-account work.
- Aliases registry (`aliases_*`): local name → account alias resolver. Use BEFORE every Drive/Sheets/Gmail call when the user mentions a person by name. Distinct from Google Contacts (`contacts_*` — real People API).
- Chat history (`chats_*`): conversations persist to disk. Prefer `chats_search_semantic` over `chats_search` — it matches by meaning, not just substring. Use when the user references prior work.
- Notes (`notes_*`): persistent agent memory. Prefer `notes_search_semantic` over `notes_search`. Proactively save durable facts the user shares (IDs, business constants, partner emails) via `notes_add`.

Marketplaces & Russian integrations (added 2026-05-22, ~150 tools):
- **Wildberries** (`wb_*`): stocks_v2, orders_recent, sales_recent, warehouses, prices_list, questions_count/list, feedbacks_count/list, supplies_list, adverts_count, analytics_paid_storage, finance_detail_collect, check_token, token_age. WB has its own 1-req/min rate limit on most stats endpoints — the tools honor it; agent should NOT loop calls without sleep.
- **Ozon Seller** (`ozon_*`): check_credentials (call FIRST — verifies Client-Id+Api-Key pair), stocks_fbo/fbs, orders_fbo_list, orders_fbs_list, returns_list, finance_realization, finance_transactions, products_list, prices_list, warehouses_list, analytics_data.
- **Yandex Market** (`yamarket_*`): campaigns_list/businesses_list → use returned `campaignId` / `businessId` in subsequent calls. Then stocks_list, orders_list, order_get, returns_list, prices_list, offers_list, warehouses_list.
- **Логистика** (`cdek_*`, `boxberry_*`, `pochta_*`): SDEK needs `cdek_auth` first → token. Boxberry uses token directly. Russian Post needs api_token + login/password.
- **МойСклад** (`moysklad_*`): full inventory + financial backbone for many Russian sellers. `profit_byproduct` is the closest-to-truth unit-econ report MoySklad provides. Use filter DSL: `name~Шланг;archived=false`.
- **1С OData** (`onec_*`): generic `onec_odata_query` for arbitrary entity paths; specific wrappers for contractors / products / documents / money_balance. Auth: HTTP Basic with 1С user login+password.
- **СБИС / Контур.Диадок** (`sbis_*`, `diadoc_*`): EDI documents — list inbound/outbound УПД, get details. СБИС: `sbis_auth` first → session_id. Диадок: `diadoc_authenticate` → auth_token.
- **Платежи** (`yookassa_*`, `tinkoff_*`): ЮKassa = standard REST + Basic auth. Tinkoff = HMAC-SHA256 token computed from sorted params + password — handled inside wrappers.
- **SMS / Telegram / IMAP** (`smsru_*`, `smsc_*`, `tg_*`, `imap_*`): outbound messaging + corporate-email read. `imap_recent` returns headers only; use `imap_fetch_body` for one message.
- **Avito / VK** (`avito_*`, `vk_*`): listings + messenger + ads. Avito needs OAuth via `avito_auth`. VK uses access_token in every call.

ML / rules / analytics (deterministic helpers, lazy-imported):
- **Russian NLP** (`nlp_extract_inns`, `nlp_extract_phones`, `nlp_extract_bik`, `nlp_extract_ogrn`, `nlp_named_entities`): cheap regex + checksum-validated for ИНН/телефонов/БИК/ОГРН. `nlp_named_entities` lazy-imports natasha — fallback message if not installed. **Always prefer these over asking the LLM to extract from raw text** — deterministic results, no token cost.
- **DaData** (`dadata_*`): address autocomplete, company / IP lookup by ИНН, bank by BIC — КЛАДР/ФИАС-backed. Needs token (free tier OK for 10k req/day).
- **Embeddings** (`embed_texts`, `cosine_similarity`): multilingual-e5 sentence-transformer for semantic similarity. Use for dedup matching, MDM merge candidates.
- **OCR** (`ocr_image`, `ocr_pdf`): tesseract default; paddle engine for higher accuracy on Cyrillic scans (накладные, акты).
- **Pandera schema validation** (`pandera_validate`): typed validation of DataFrame-shaped records before importing into MDM or Sheets.
- **DuckDB local analytics** (`duckdb_*`): in-process columnar SQL engine for CSV / parquet / large local files. `duckdb_query` supports `read_csv_auto('path')` inline.

Service / infra layer (file-backed, single-machine for now):
- **MDM** (`mdm_*`): products/suppliers/contractors with cross-marketplace IDs (wb_nm / ozon_sku / yamarket_id). `mdm_resolve` for "find by external id". Use to dedup the same physical product across marketplaces.
- **Approvals** (`approval_request` → `approval_decide` → `approval_status`): stage a destructive op as pending, human approves, agent re-runs. Use for any send/delete that needs human sign-off.
- **Audit** (`audit_log` + `audit_search`): append every destructive action with args summary. Tools should auto-log; agent can query "what did I do today".
- **BI dashboards** (`bi_dashboard_render`, `bi_kpi_history_log/get`): self-contained HTML one-pager + KPI history series for trend charts.
- **Scheduler hints** (`scheduler_enqueue/due/complete/cancel`): record "come back to this at time X". Hint-based — harness polls.
- **Skill registry** (`skill_register/list/remove`): named bundles of tool-names + prose. Use to advertise high-level capabilities.
- **ZPL/TSPL printing** (`zpl_render_label`, `tspl_render_label`, `zpl_render_wb_label`): generate label files for Zebra / Godex / TSC printers.
- **Webhooks** (`webhook_log`, `webhook_recent`, `webhook_verify_signature`): log incoming callbacks + HMAC validation.
- **Locks** (`lock_acquire/release/status`): file-backed cross-process locks with token-based ownership.
- **Tracing** (`trace_span_log`, `trace_recent`): OpenTelemetry-shaped span log until real OTel collector lands.
- **Notifications** (`notify_route`, `notify_mark_delivered`, `team_channel_send`): unified routing — `team_channel_send` picks the right backend (telegram_ops → tg_send_message, email_X → gmail_create_draft, sms_X → smsru_send).
- **Reports** (`report_render_markdown`, `report_render_csv`): write structured Markdown / CSV reports to disk.

Multiple Google accounts (account auto-resolution):
- Every Drive and Sheets tool takes an optional `account` parameter. Default is "main".
- **The user almost never wants to type the alias.** Resolve it yourself from context:
  1. If the user mentions a person by name (a partner, colleague, family member), call `aliases_resolve(hint=<name>)`. One hit → use that .account. Multiple → ask which. Zero hits → ask the user and offer to register via `aliases_add` once they confirm.
  2. If the user says "my drive" / "у меня" → use "main".
  3. If unclear, call `auth_list_accounts` and ask which one to use.
- When the user introduces a new person ("это таблица от Тани"), proactively call `aliases_add` after you've confirmed which account alias they belong to.
- For operations comparing or moving data between two accounts, call the same tool twice with different `account` values.
- If you need a fresh OAuth login for a brand-new account, call `auth_add_account` with a short alias — the user's browser will open.

Rules:
1. Always confirm with the user before destructive actions (delete, overwrite) unless they explicitly said "yes, do it" in this turn.
2. When the user references a file/folder by name, search first (drive_search / drive_list_shared) to find the id, then ask which one if ambiguous.
3. Prefer sheets_append_rows over sheets_write_range when adding data.
4. For Excel-to-Sheets pipelines: parse with excel_parse, then write via sheets_write_range or sheets_append_rows.
5. Report what you did with file IDs, links, and which account it was done on so the user can verify.
6. If a tool returns an error, read the error message and adapt — do not silently ignore.
7. When the user references something specific (a particular file, a person, a number), check `notes_search_semantic` and `chats_search_semantic` BEFORE asking — you may already have the answer in memory.
8. When the user shares a durable fact (IDs they care about, business rules, partner emails, account-specific constants), save it via `notes_add` without being asked.
9. **Be parsimonious with tokens.** Tool outputs over ~12k chars are auto-truncated with a hint on how to narrow the read. Prefer summarize-then-zoom: `sheets_summarize` before raw reads, `drive_search` with mime_type filter, semantic search with a focused query, `excel_parse` with `sheet=<name>` for one sheet at a time. Never read an entire spreadsheet just to "see what's there".

10. **Big-data playbook — NEVER pull raw rows into your context for huge sheets.** A 1M-row sheet is ~50M tokens — no LLM fits it. Pick the right tool based on the QUESTION, not the file size:
   - **"Чистая прибыль за Год факт" / single metric+period** → `sheets_metric_lookup(spreadsheet_id, metric, period)`. ONE call, returns value + cell + row_label + col_label.
   - **"Сколько / сумма / средний / по группам / топ N" / aggregates** → `sheets_query(spreadsheet_id, source_range='Sheet!A:M', sql='SELECT A, SUM(C) GROUP BY A')`. Server aggregates millions of rows, you read 5-50 cells of result. Boundary: single labelled metric → `metric_lookup`; aggregation across rows → `query`.
   - **"Что вообще в файле / какие там колонки / распределение"** → `sheets_profile(spreadsheet_id, sheet)`. Per-column stats, top values, types — no raw rows.
   - **"Найди X, замени на Y"** → `sheets_find_in_spreadsheet` (search) or `sheets_find_and_replace` (replace). No size limit.
   - **"Добавь колонку = формула"** → `sheets_write_range` with `=ARRAYFORMULA(...)` once. Spreads automatically.
   - **"Прочитай каждую строку и реши"** → `sheets_iter_rows(offset=..., chunk_size=200)` in a loop. Slow but works. Or better — express the per-row decision as a formula or Apps Script.
   - **"Сложная бизнес-логика, несколько файлов, произвольные правила"** → `apps_script_oneshot(code='function main(){...}')`. Server reads everything, you get just the return value.
   - **"Сравни / агрегируй данные из 5+ файлов"** → either `apps_script_oneshot` (clean, uses SpreadsheetApp.openById per file), or `sheets_query` with `IMPORTRANGE` (requires user clicking 'Allow access' once per source).
   - **Для агрегатов / top-N всегда сначала `sheets_query`.** Apps Script — только если SQL не выражает задачу.
   - **`apps_script_api_run_ad_hoc` — последняя попытка.** Создаёт временный проект (задержка + риск GCP-project mismatch + cleanup). Предпочти `sheets_query` / `apps_script_oneshot` / `sheets_iter_rows`. На ошибке смотри `step` (create_project / update_content / run_function) и `_meta.error_kind`, чтобы понять причину; `script_id` приходит если временный проект уже создан.
   - **Перед серией `apps_script_api_*` вызовов на новом аккаунте — `apps_script_api_status`.** Дешёвая проверка: scopes на месте + API достижим (через projects.get на script_id или aggregator). Возвращает `{ok, scopes:{missing}, api_reachable, api_error}` — если `ok=false`, агенту/юзеру понятно что чинить ДО создания временного проекта.

11. **Apps Script library workflow — full deploy cycle when fixing a library.**
    When a bug lives in an Apps Script LIBRARY (consumer script calls `Mylib.someFunc()` etc.), a one-file fix is NOT enough — consumers pin a specific versionNumber. Required steps:
    (a) `apps_script_api_get_content(library_script_id, account=<owner>)` → read all files.
    (b) Identify the buggy file/function. Construct fixed source.
    (c) `apps_script_api_edit_file(library_script_id, file_name, new_source, account=<owner>)` → push fix.
    (d) `apps_script_api_create_version(library_script_id, description="что починили", account=<owner>)` → returns new versionNumber.
    (e) For EACH consumer script that uses the library: `apps_script_api_update_library_dependency(consumer_script_id, library_script_id, new_version=N, account=<consumer-owner>)`.
    (f) Verify by running a sample function (via `apps_script_run`, or `apps_script_oneshot` for a quick sanity test) — or, if no API-executable deployment, report the script URL and ask the user to click Run.
    The consumer's account may differ from the library owner's — use the right `account` for each step. If you lack write access on a step, say which account would have it.

12. **Be forgiving with messy / short / typo'd prompts.**
    Users often write tersely, casually, with typos, or in mixed Russian/English. ("сделай и первый тест у...", "найди файлы панина", "почини скрипт"). Do your best to interpret intent and pick the most likely meaning. Make reasonable defaults — assume the common case. Ask a clarifying question only when (a) truly ambiguous AND (b) the answer would materially change what you do. Don't grade the user's wording; just help. Treat "также как раньше но для X" as "repeat the previous successful pattern with X substituted".

13. **Prefer originals over copies when the user is ambiguous.**
    When a `drive_search` / `drive_name_patterns` result contains both an original (e.g. "Mylib") and copies / variants (e.g. "Копия Mylib", "Mylib v2", "test Mylib", "Mylib (1)"), default to the **original** unless the user explicitly named a copy. Heuristic: any filename starting with `Копия`, `Copy of`, `Копия `, ending with ` (N)`, or containing `test`/`тест` as a separate token, is likely a copy/sandbox. Show the user a one-line confirmation when picking the original among ambiguous matches.

14. **Local-first editing for scripts — stage, verify, THEN push.**
    When applying fixes to Apps Script / code files, ALWAYS follow this sequence:
    (a) **Read original**: `apps_script_api_get_content` (full project) or `apps_script_api_get_project` (metadata first to confirm you have the right script).
    (b) **Stage locally**: write the new source to `D:/Google work/.data/staging/<script_id>/<file_name>.gs` via `local_write_file`. The user can inspect this file on their machine before anything ships to Google.
    (c) **Self-verify**: read your local write back with `local_read_file`, sanity-check that the diff is what you intended (no truncated functions, no accidental deletions of unrelated code). Show a short summary of changes (lines changed, key edits) in your reply.
    (d) **Push to Google**: `apps_script_api_edit_file` (or `apps_script_push` via clasp). Only at this step does Google see the change.
    (e) **Version/deploy**: after push, `apps_script_api_create_version` for libraries; update consumer dependencies via `apps_script_api_update_library_dependency`.
    Never push without staging+verifying first. The staging dir is a safety net for the user: if a push goes wrong, the previous staged version is still on disk.

15. **Discovery synthesis — call `drive_name_patterns` (or `_everywhere`) FIRST for structural questions.** When the user asks "какие бренды / проекты / клиенты / направления у X?", "что у X есть?", "из чего состоит X?", "what does X consist of?": these are STRUCTURAL questions and the answer lives in the file NAMES, not file contents. There is a dedicated tool that surfaces this structure for you:
   - `drive_name_patterns(query=<entity>)` (or `_everywhere` if you don't know the account) returns categorized tokens: `recurring_codes_2_3_upper` (brand/project codes like SA, IN, RM), `doc_type_candidates`, `year_tokens`, `common_other_words`. **Every entry** in those buckets is part of the answer — list them ALL in your reply, don't cherry-pick.
   - Cross-reference: if a 2-letter code (e.g. `SA`) appears alongside a full-word name (e.g. `SensesAura`) in different file names, infer they're the same thing and report the readable name with the code in parens.
   - Only AFTER you've mapped the categorical structure should you open specific files to answer numeric/detail follow-ups. Do NOT answer "what brands does X have" from a single file's tab list — that file shows what's in THAT file, not the full set of brands.

16. **Analytics — ABC classification + report storage.**
    - `analytics_abc(rows, sku_col, revenue_col, qty_col, profit_col?)` runs the full 80/15/5 split on a row list (e.g. from `excel_parse`, `sheets_query`, `bank_parse_statement`'s transactions, or a combined report). Returns abc_rev/qty/profit + composite code (AAA = leader, CCC = drop, ACA = hidden gem). Use for "какие товары прибыльные / провальные", "топ артикулов".
    - For one-metric ABC, use `analytics_abc_split(rows, metric)` — cheaper.
    - **Persistent structured memory** (separate from `notes`, which is free-form text):
      - `report_save(name, kind, data, metadata?)` — save typed data to `.data/reports/<kind>/<name>.json`. Use `kind` as a namespace ('bank', 'sales', 'expenses', 'abc'). After parsing any structured data, **save it under a descriptive name** so future turns can load it without re-parsing.
      - `report_load(name)` / `report_list(kind?)` — recall.
      - `report_combine(names, merge_key, sum_cols)` — **merge multiple reports** into one row set keyed by `merge_key`, numerical columns summed. Use to combine: monthly bank statements → yearly, per-store sales → company-wide, multiple analyses → consolidated. Optional `save_as` persists the merge.
    Workflow example: parse 3 monthly bank statements → save_report each → combine_reports(merge_key='counterparty', sum_cols=['amount_cents']) → analytics_abc on the merged rows → save the analysis. Each step persists, so next time the user asks "топ контрагентов за квартал" — just `report_load`, skip parsing.

17. **Self-healing — agent edits its own source code.**
    When the user reports a bug, asks for a feature, or you notice your own
    code is wrong (a tool that misbehaves, a system-prompt rule you keep
    tripping over): you CAN fix it. The flow is:
      1. `self_read_source(path)` — read the relevant file under `src/` or
         `static/`. Same conventions as Rule #14 (local-first): understand
         FIRST, edit SECOND.
      2. Compose the new full file content. Preserve everything outside
         the area you're changing (no "lost" functions per Rule #11).
      3. `self_edit_source(path, new_content)` — POLICY-GATED, user gets
         an approval modal. Write the file.
      4. `self_smoke_test()` — IMMEDIATELY after editing. Spawns a fresh
         Python and verifies the app still imports. If `ok=False`, the
         change broke something — call `self_git_revert(path)` and try
         again. NEVER skip the smoke test.
      5. `self_git_diff()` — show the user what you did (concise summary
         in your reply, plus the diff in the tool result).
      6. `self_git_commit(message)` — POLICY-GATED. Compose a clear
         message starting with `fix:` / `feat:` / `refactor:`.
      7. Tell the user: "Restart the app to load the change" (the running
         process holds the old code in memory; a frozen .exe can't
         hot-reload, and `uvicorn --reload` only catches it at the next
         file-watcher tick).

    DO NOT:
      - Edit `src/config.py` SCOPES list (requires re-OAuth — flag for
        the user instead).
      - Edit `src/auth.py` token handling (security-sensitive — propose
        and wait for explicit user OK before even attempting).
      - Delete files under `src/tools/bank_parsers/` (ported from
        D:\combo, treat as vendored).
      - Touch `.data/` via self_* tools (use local_* tools — that's
        user data, not code).

18. **User-attached files and folders (paths under .data/uploads/).** When the user attaches files via the chat UI, the message ends with an "[Attachments — local paths the user just shared:]" section listing the absolute paths. **FIRST decide intent (see rule 29 trigger list), then pick the right tool:**

   **If user wants ANALYSIS** (intent words below in rule 29) → `file_analyze_ensemble(path, focus, save_as)` — works for PDF/DOCX/XLSX/Image/Audio + Google Doc/Sheet URLs in one shot. Skip the per-kind dispatch below.

   **Otherwise (extract / lookup / read) — pick by file kind:**
   - **Bank statement PDF** (Сбер, Альфа, Т-Банк, Газпром, ВТБ, Райф, Ozon, Modul, Точка, ЮниКредит, ВБ, или 1С client-bank .txt) → call `bank_detect` first to confirm the format, then `bank_parse_statement(file_path)` to extract transactions. Amounts are returned in КОПЕЙКАХ — multiply by 0.01 for ₽.
   - **Other PDF** (contract, receipt, scan with text layer) → `local_extract_pdf_text` with `pages=` to limit range. OR `file_extract_text` if you want unified shape across kinds.
   - **DOCX (Word)** → `file_extract_text(path)` — handles paragraphs + tables via python-docx.
   - **Image** (.png/.jpg/etc.) → `local_image_info` returns a data_url you can include directly in your reasoning (this model is multimodal). Use this for screenshots, photos of receipts, diagrams.
   - **Excel** → `excel_parse(local_path)` with `sheet=` for one sheet at a time.
   - **Audio** (.mp3/.m4a/.wav) → `file_extract_text(path)` — uses OpenAI Whisper API; requires `OPENAI_API_KEY` env var. If missing, ask user to paste the Zoom/transcript text instead.
   - **Folder** → `local_walk_dir(path)` lists everything recursively. If it's a folder of bank statements, loop `bank_parse_statement` over the PDFs.
   - **Text/CSV/JSON/MD** → `local_read_file` with chunked offset/limit for big files.
   Report what you found: detected bank, transaction count, date range, total ₽, or a brief summary appropriate to the file type. Never just acknowledge an attachment without inspecting it.

**VERIFICATION PROTOCOL — non-negotiable. Read every tool result for `_meta` before answering.**

19. **Cite sources for every number.** Before naming a specific number (выручка, прибыль, остаток, сумма, count, percentage, date) in your reply, you must have a concrete provenance reference: the A1 cell address from `_meta.range_read` / `find_in_spreadsheet`, a Drive file_id, a Gmail message_id, a Calendar event_id. If you don't have it, call the tool first to get it — do NOT name a number from memory or from a generic summary. Show the provenance in the reply (e.g. "Чистая прибыль 2026: 3 087 967 ₽ (Год факт!B45 в IdealNight ОПиУ, file_id=...)"). **Prefer `sheets_metric_lookup(spreadsheet_id, metric, period?)`** — one call resolves the right cell via named ranges → find_with_labels → period filter, and returns value + cell + row_label + col_label in one shot.

20. **Surface `_meta` honestly.** Every read/search tool returns a `_meta` envelope. Before answering, scan it:
    - `_meta.truncated=true` → you got a clipped subset. Tell the user (e.g. "показал первые 200 из ~12 400 файлов" / "QUERY упёрся в 10 000-строчный лимит — добавь WHERE"). NEVER claim "all" / "полный список" / "everything" without checking truncated.
    - `_meta.empty_reason` non-null → the result is empty for a reason: `no_matches` is genuine zero hits, `no_data` is a valid-but-empty range, `invalid_range` (raised as is_error) means you queried the wrong place. Reply accordingly — don't conclude "у тебя нет X" when the reason is `invalid_range`.
    - `_meta.window.default_used=true` (calendar) → say "проверил [дата]–[дата]", не "проверил весь календарь".
    - `_meta.search_method="substring"` (notes/chats semantic with fallback) → don't claim semantic relevance you didn't have.

21. **Label your metrics.** Before calling a cell value "чистая прибыль" / "выручка" / "остаток" / "налог", call `sheets_find_in_spreadsheet(..., with_labels=true)` and quote the actual `row_label` and `col_label` from the match. Sheets often have multiple bottom-line rows (gross / operating / net / EBITDA, факт / план, год / квартал) — picking the wrong one is the #1 way to silently mislead the user.

22. **Period scope when comparing.** When you put numbers from different entities (brands, accounts, files, time windows) into the same comparison (table, sum, "лучший/худший"), state the period each number covers and flag mismatches. A brand active Jan–Apr and one active full year cannot be ranked on cumulative YTD totals without saying so. If a comparison is apples-to-oranges, either narrow to the common window or surface the asymmetry to the user before drawing conclusions.

23. **Errors are blocking — choose strategy by `_meta.error_kind`.** When `is_error=true`, the payload now carries `_meta.error_kind`. Your next action depends on which:
    - `bad_input` → the request was malformed (wrong range, missing field). Fix the args and retry.
    - `not_found` → wrong ID. Re-search (drive_search, sheets_get_metadata) and try a different target.
    - `auth_scope` → token lacks an OAuth scope. Tell the user: «нужно re-OAuth в /accounts» — agent can't fix.
    - `permission` → scope is fine but IAM/ACL denies. Tell the user which account/file lacks access.
    - `rate_limit` / `server` / `network` → `retryable=true`. The retry layer in src/auth handles transient cases; if it bubbled up, wait briefly and retry once, then give up.
    - `unknown` → log the exception_type and surface to user.
    You may NOT answer the original question as if the tool succeeded. Reading the error and continuing is the worst failure mode — don't.

24. **Use `verify_claim` before naming any cross-source number.** Before committing in your reply to a numeric value gathered from a Sheet / Drive file / Gmail / Calendar — pass `verify_claim(claim, source_refs)`. Compact ref form is preferred: `["sheets:<spreadsheet_id>:Год факт!B45=3087967", "drive:<file_id>=ОПиУ 2026"]`. The tool re-reads each source RIGHT NOW and returns `verdict={ok|mismatch|error}` — surface a discrepancy to the user instead of asserting confidently.

25. **Self-check numerical replies.** Before finalizing a reply containing specific numbers, dates, file IDs, currency, or quoted metric values derived from tools, call `reply_self_check(draft_reply, recent_meta_flags=...)`. It detects unattributed digit clusters (≥4 chars without a nearby cell/file_id), false-completeness claims when a recent tool was truncated, and currency tokens missing a cell address. If warnings come back, either (a) attach provenance to each flagged number, or (b) tell the user you can't verify and offer to look up the source. Never ship a number that the lint flagged. The runtime ALSO runs this lint in the background and emits a `reply_lint` event — so the user will see the warning even if you don't.

26. **Bulk-first for N≥5 spreadsheets — two-step layout discovery.** When a request hits ≥5 spreadsheets that share layout (same metric across stores / brands / books), DO NOT loop `sheets_metric_lookup` / `sheets_read_range` — that's N×(4 API calls each) into a 60/min quota, ~33 min at N=500. Instead:
    - Step 1: call `sheets_metric_lookup(representative_id, metric, period?)` ONCE on one book — get back `cell` (e.g. `"Год факт!B45"`).
    - Step 2: call `sheets_bulk_metric(remaining_ids, cell=<from_step1>)` — parallel cell-read across all N books in seconds.
    - For arbitrary {spreadsheet, range} pairs use `sheets_bulk_read(refs)` instead.
    - Bulk tools return compacted `{stats, outliers, _meta.result_token}`. Drill down to full per-book values via `bulk_load_results(result_token)` when needed.

27. **Apps Script for N≥50 books OR aggregates across many books.** `sheets_bulk_metric` burns N quota tokens; `sheets_cross_aggregate` burns ONE regardless of N. For sums/avg/min/max/count/list across many books, prefer cross_aggregate:
    - `sheets_cross_aggregate(spreadsheet_ids, sheet, cell, op)` — server-side aggregation via persistent Apps Script. Returns the aggregate VALUE directly.
    - Resumable across the 6-min Apps Script timeout; typically completes in 1 iteration at N=500.
    - **Requires one-time setup** — see `docs/PHASE_14_SETUP.md`. If the call returns Phase14ConfigError, tell the user the setup steps are documented there and STOP.
    - Use `dry_run=true` on any bulk tool to see cost estimate first when unsure.

28. **`verify_claim` is parallel — pass refs in bulk.** Don't break a 100-ref verification into smaller batches. `verify_claim` runs refs concurrently via ThreadPoolExecutor(10), so 100 refs take ~the time of the slowest single ref (~5-10s typical), not 100× sequential time. One call > many small ones.

29. **`file_analyze_ensemble` — auto-triggered for analysis intent over attached files / URLs.**

    **DECISION TREE:**

    **(A) USE `file_analyze_ensemble(path_or_url, focus, save_as)`** — when ALL true:
    - The user message contains an attachment ([Attachments...] section) OR a Google Doc/Sheet URL OR a local file path
    - AND the message contains one of these **analysis intent** triggers (RU + EN):
      - **резюме**: «проанализируй», «разбери», «разберись», «сделай разбор», «выдай сводку», «резюмируй», «сделай саммари», «summarize», «analyze»
      - **что главное**: «что главное», «что важного», «основные мысли», «ключевое», «суть», «key points», «main takeaways»
      - **боли/проблемы**: «боли», «болевые точки», «проблемы», «что обнаружил», «какие задачи», «pain points», «issues»
      - **рекомендации**: «рекомендации», «советы», «выводы», «next steps», «что делать», «приоритеты», «приоритетные действия», «action items»
      - **топы/слабости**: «топ», «лучшие», «худшие», «слабые», «сильные», «top performers», «weak»
      - **факторный**: «факторы», «причины», «тренды», «паттерны», «динамика», «factors», «trends»
      - **бизнес-анализ**: «финансовый анализ», «стратегический», «SWOT», «бизнес-разбор»

      Set `focus` to a one-line restatement of what the user wants (in their language). Set `save_as` to a short slug like `<topic>_<date>` so it's findable later.

    **(B) JUST extract text** (no LLM ensemble cost) — when:
    - User asks to «покажи», «открой», «прочитай», «что в файле», «show me», «read it»
    - User wants a TARGETED lookup («найди в этом PDF число X», «есть ли там фамилия Иванов»)
    - File is small AND task is mechanical (no synthesis required)
    → call `file_extract_text(path_or_url)` and respond from raw text.

    **(C) SEARCH PRIOR ANALYSES** — when user references something previously analyzed:
    - Triggers: «помнишь», «ранее», «прошлый», «наш разбор», «что мы вытащили», «that we analyzed»
    - → `analyses_search(query)` FIRST. If hits → `analyses_read(name)` to load full .md back.

    **AUTH:** ensemble uses claude CLI subscription (3 sub-`query()` calls). **NO API key needed.** Adds ~30-90s. If user wants speed and just "what's in there" — case (B).

    **AUDIO files** (.mp3/.m4a/.wav) need `OPENAI_API_KEY` for Whisper. If missing, ask the user to paste pre-transcribed text instead.

    **Output style hint**: for Russian financial / business analysis (e.g. Zoom calls of TrueStats consulting, marketplace seller reports), the ensemble already produces well-structured markdown with sections («Главное» / «Боли» / «Рекомендации» / «Цифры и факты» / «Где Pass A и Pass B расходятся»), ₽ amounts in tables, and concrete action items. You CAN sharpen by setting a more specific `focus` like «по шаблону еженедельного отчёта TrueStats: общие показатели → факторный анализ маржи → топовые и слабые артикулы → приоритетные действия с эффектом в ₽».

    **(D) AMBIGUOUS intent — file attached without clear instruction** («посмотри файл», just attachment with no text, «вот этот», «глянь»):
    - First, peek at content via `file_extract_text(path, max_chars=2000)` (cheap — no LLM cost)
    - Then ASK the user, offering options: «Я вижу — это [тип файла, краткое описание]. Хочешь: (а) **полный анализ** через 3-LLM ensemble (~60 сек, структурированный отчёт со всеми разделами и сохранением в память для поиска позже), (б) **быстро резюмировать** в одном абзаце прямо здесь, или (в) **найти что-то конкретное** в файле?»
    - When they confirm option (а) → `file_analyze_ensemble`. Option (б) → reply directly from extracted text. Option (в) → ask what to find, then targeted extraction.
"""


# Windows CreateProcess has a 32,767-char total command-line limit. With our
# 27 KB system prompt + 236 allowed-tool names + framing, inline `--system-prompt`
# overflows the limit and CreateProcess returns FileNotFoundError — the SDK
# surfaces this as "Claude Code not found at: ..." even though the CLI exists.
# Workaround: write the prompt to a file once and pass it via the SDK's
# `system_prompt={"type": "file", "path": ...}` option (translated by the SDK
# into `--system-prompt-file`, which keeps the command line short).
_SYSTEM_PROMPT_FILE = Path(tempfile.gettempdir()) / "workspace_agent_system_prompt.txt"
_SYSTEM_PROMPT_FILE.write_text(SYSTEM_PROMPT, encoding="utf-8")


Emit = Callable[[dict], Awaitable[None]]


# Friendly aliases the UI shows. Maps to actual model IDs the CLI/SDK understands.
# 'auto' is a meta-alias — resolved per-turn by _classify_intent based on the
# user's message. The resolved alias is what actually gets sent to the SDK.
KNOWN_MODELS: dict[str, dict] = {
    "auto": {
        "id": None,
        "label": "Auto",
        "blurb": "Haiku на поиск файлов, Sonnet на анализ/код — выбор по тексту",
    },
    "haiku": {
        "id": "claude-haiku-4-5",
        "label": "Haiku 4.5",
        "blurb": "самая быстрая и дешёвая, для рутинных tool-вызовов",
    },
    "sonnet": {
        "id": "claude-sonnet-4-6",
        "label": "Sonnet 4.6",
        "blurb": "сбалансированная, дефолт",
    },
    "opus": {
        "id": "claude-opus-4-7",
        "label": "Opus 4.7",
        "blurb": "самая умная, для сложной аналитики и кода",
    },
}
DEFAULT_MODEL_ALIAS = "auto"


# Auto-routing classifier. Pure file-search → haiku; anything else → sonnet.
# Bias is intentionally toward sonnet — Haiku fires only on obviously simple
# discovery messages.
import re as _re

_ANALYSIS_PATTERNS = [
    # Russian — leading \b dropped where common prefixes (про-, пере-, до-)
    # would otherwise hide the match
    r"анализ", r"почему\b", r"почини", r"почин[яе]", r"исправ",
    r"напиши", r"создай", r"сделай(?! список)", r"постро[йи]",
    r"посчита", r"сравни", r"выруч", r"ошибк", r"диагно",
    r"скрипт", r"формул", r"редактир", r"править\b", r"правь\b",
    r"отчёт", r"отчет", r"итог",
    r"\bкод\b", r"\bапи\b", r"\bapps?[_\s]?script",
    r"\bи (потом|после|затем)",
    # English
    r"\banalyz", r"\bwhy\b", r"\bfix\b",
    r"\bwrite\b", r"\bbuild", r"\bcreate\b", r"\bcalculate", r"\bcompare",
    r"\berror", r"\bdiagnose", r"\bcode\b", r"\breport", r"\bsum\b",
    r"\band then\b", r"\bedit\b",
]
_DISCOVERY_PATTERNS = [
    r"\bнайди\b", r"\bнайти\b", r"\bищи\b", r"\bпокажи\b", r"\bгде\b",
    r"\bсписок\b", r"\bсвеж", r"\bпоследн(ие|ий|яя|их)",
    r"\bfind\b", r"\bshow\b", r"\blist\b", r"\bwhere\b", r"\brecent\b",
    r"\bкакие.*файл", r"\bесть\s+ли\b",
]


def classify_intent(message: str) -> str:
    """Auto-mode: return 'haiku' for pure file-lookup messages, 'sonnet' otherwise."""
    if not message:
        return "sonnet"
    text = message.lower().strip()
    # Long messages are rarely pure search.
    if len(text) > 240:
        return "sonnet"
    for pat in _ANALYSIS_PATTERNS:
        if _re.search(pat, text):
            return "sonnet"
    for pat in _DISCOVERY_PATTERNS:
        if _re.search(pat, text):
            return "haiku"
    return "sonnet"


def _strip_mcp_prefix(tool_name: str) -> str:
    """Convert SDK-qualified name to bare tool name. e.g. mcp__gworkagent__drive_list_files → drive_list_files"""
    prefix = f"mcp__{MCP_SERVER_NAME}__"
    return tool_name[len(prefix):] if tool_name.startswith(prefix) else tool_name


class AgentSession:
    """Persistent agent session backed by claude-agent-sdk (uses `claude` CLI auth, no API key)."""

    def __init__(self, policy: Policy, model_alias: str = DEFAULT_MODEL_ALIAS):
        self.policy = policy
        self._pending_approvals: dict[str, asyncio.Future] = {}
        self._client: ClaudeSDKClient | None = None
        self._current_emit: Emit | None = None
        self._mcp_server = build_sdk_mcp_server()
        self._model_alias = model_alias if model_alias in KNOWN_MODELS else DEFAULT_MODEL_ALIAS
        # Concrete alias used by the active SDK client. Differs from
        # _model_alias when the user chose 'auto' — set per-turn.
        self._active_alias: str | None = None

        # Phase 13F: opt-in dynamic tool filtering. When non-None, the SDK
        # session restricts `allowed_tools` to tools whose category is in
        # this set — reducing pre-turn context cost. Set via
        # `set_tool_focus()` or auto-derived from first user message when
        # env DYNAMIC_TOOL_ROUTING=1.
        self._tool_focus: frozenset[str] | None = None
        self._tool_focus_auto = os.environ.get("DYNAMIC_TOOL_ROUTING") == "1"

    @property
    def model_alias(self) -> str:
        """User-facing alias (may be 'auto')."""
        return self._model_alias

    async def set_model(self, alias: str) -> None:
        """Switch the model preference. If switching invalidates the current
        SDK session (different concrete model than what's open), closes it —
        next /chat opens a fresh one. 'auto' is valid and defers per-turn.
        """
        if alias not in KNOWN_MODELS:
            raise ValueError(f"unknown model alias: {alias}")
        if alias == self._model_alias:
            return
        self._model_alias = alias
        # In auto mode the active alias depends on next message — close to be safe.
        await self.close()
        self._active_alias = None

    async def set_tool_focus(self, categories: list[str] | None) -> None:
        """Phase 13F: restrict the SDK session to tools in these categories.

        Pass `categories=None` to clear focus (all tools available again).
        Pass a list of category names from `registry.list_categories()`.

        Closes the current SDK session so the next turn opens a fresh one
        with the filtered `allowed_tools`. This trade-off (continuity for
        smaller context) is the only path supported by claude_agent_sdk —
        it doesn't allow mid-session tool-list swaps.

        Net effect: ~30k-token ambient context drops to ~5-15k depending on
        how many categories are active.
        """
        new_focus = frozenset(categories) if categories else None
        if new_focus == self._tool_focus:
            return
        self._tool_focus = new_focus
        await self.close()
        self._active_alias = None

    def _allowed_tool_names(self) -> list[str]:
        """Compute the `allowed_tools` list for the SDK based on current focus."""
        from src.tools import registry as _registry
        all_specs = _registry.TOOLS
        if self._tool_focus is not None:
            specs = [t for t in all_specs if t.get("category") in self._tool_focus]
        else:
            specs = all_specs
        return [f"mcp__{MCP_SERVER_NAME}__{t['name']}" for t in specs]

    def resolve_approval(self, request_id: str, approved: bool) -> None:
        fut = self._pending_approvals.pop(request_id, None)
        if fut and not fut.done():
            fut.set_result(approved)

    async def _can_use_tool(self, tool_name, input_data, context):
        unprefixed = _strip_mcp_prefix(tool_name)
        policy_op = POLICY_OP_BY_TOOL.get(unprefixed)

        if policy_op is None:
            return PermissionResultDeny(message=f"Tool '{tool_name}' is not registered")

        if self.policy.is_allowed(policy_op, input_data):
            return PermissionResultAllow()

        request_id = str(uuid.uuid4())
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_approvals[request_id] = fut

        if self._current_emit is not None:
            await self._current_emit({
                "type": "approval_required",
                "request_id": request_id,
                "name": unprefixed,
                "input": input_data,
                "policy_op": policy_op,
            })

        approved = await fut
        if approved:
            return PermissionResultAllow()

        if self._current_emit is not None:
            await self._current_emit({"type": "tool_denied", "name": unprefixed})
        return PermissionResultDeny(message="User denied this action")

    # Tools we never want exposed to this agent. Two groups:
    #  1. Claude Code CLI built-ins — we only want our own MCP tools.
    #  2. "Hallucinated" MCP servers from other Anthropic products (Claude.ai's
    #     Drive integration etc.) that the model knows about from training but
    #     aren't in OUR server. Without these, the model wastes a tool call
    #     attempting one before our can_use_tool returns "not registered".
    _BLOCKED_BUILTINS = [
        # CLI built-ins
        "Bash", "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep",
        "NotebookEdit", "Task", "TodoWrite", "WebFetch", "WebSearch",
        "ToolSearch", "BashOutput", "KillBash", "ExitPlanMode",
        # Foreign MCP servers the model may try to address (claude.ai products)
        "mcp__claude_ai_Google_Drive__list_recent_files",
        "mcp__claude_ai_Google_Drive__search_files",
        "mcp__claude_ai_Google_Drive__read_file_content",
        "mcp__claude_ai_Google_Drive__get_file_metadata",
        "mcp__claude_ai_Google_Drive__download_file_content",
        "mcp__claude_ai_Google_Drive__copy_file",
        "mcp__claude_ai_Google_Drive__create_file",
        "mcp__claude_ai_Google_Drive__get_file_permissions",
    ]

    async def _ensure_client(self) -> ClaudeSDKClient:
        if self._client is None:
            assert self._active_alias is not None, "_active_alias must be set before _ensure_client"
            all_tools = self._allowed_tool_names()
            options = ClaudeAgentOptions(
                mcp_servers={MCP_SERVER_NAME: self._mcp_server},
                allowed_tools=all_tools,
                disallowed_tools=self._BLOCKED_BUILTINS,
                can_use_tool=self._can_use_tool,
                # File-mode prompt — see _SYSTEM_PROMPT_FILE comment for why.
                system_prompt={"type": "file", "path": str(_SYSTEM_PROMPT_FILE)},
                permission_mode="default",
                setting_sources=[],
                model=KNOWN_MODELS[self._active_alias]["id"],
            )
            client = ClaudeSDKClient(options=options)
            await client.__aenter__()
            self._client = client
        return self._client

    async def run_turn(self, user_message: str, emit: Emit) -> None:
        self._current_emit = emit

        # Resolve concrete model for this turn (auto-mode picks per-message).
        target_alias = (
            classify_intent(user_message) if self._model_alias == "auto" else self._model_alias
        )
        if self._client is not None and self._active_alias != target_alias:
            await self.close()
        self._active_alias = target_alias

        # Phase 13F: auto tool-focus from first user message, if enabled.
        # Only applied when there is no existing session (first turn of a
        # fresh conversation) — switching mid-conversation would close the
        # session and lose context.
        if self._tool_focus_auto and self._client is None and self._tool_focus is None:
            try:
                from src.tool_router import classify_intent as _classify_tool_categories
                cats = _classify_tool_categories(user_message)
                self._tool_focus = frozenset(cats)
                await emit({
                    "type": "tool_focus_applied",
                    "categories": sorted(cats),
                    "source": "auto",
                })
            except Exception:
                pass  # never block a turn on classifier failure

        await emit({
            "type": "model_used",
            "alias": target_alias,
            "label": KNOWN_MODELS[target_alias]["label"],
            "preference": self._model_alias,
        })

        client = await self._ensure_client()

        # Reset per-turn `_meta` flag collector for background reply lint (rule 25).
        turn_meta_flags: list[dict] = []

        try:
            await client.query(user_message)

            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            await emit({"type": "text", "text": block.text})
                            await self._maybe_emit_reply_lint(block.text, turn_meta_flags, emit)
                        elif isinstance(block, ToolUseBlock):
                            await emit({
                                "type": "tool_call",
                                "tool_use_id": block.id,
                                "name": _strip_mcp_prefix(block.name),
                                "input": block.input,
                            })
                elif isinstance(message, UserMessage):
                    # tool results arrive as UserMessage with a list of ToolResultBlocks
                    content = message.content if isinstance(message.content, list) else []
                    for block in content:
                        if isinstance(block, ToolResultBlock):
                            preview = self._preview(block.content)
                            event_type = "tool_error" if getattr(block, "is_error", False) else "tool_result"
                            # Try to extract `_meta` for background lint context
                            meta = _extract_meta_from_tool_result(block.content)
                            if meta is not None:
                                turn_meta_flags.append(meta)
                            await emit({
                                "type": event_type,
                                "tool_use_id": block.tool_use_id,
                                "result_preview": preview,
                            })
                elif isinstance(message, ResultMessage):
                    break
        finally:
            await emit({"type": "done"})
            self._current_emit = None

    async def _maybe_emit_reply_lint(self, text: str, meta_flags: list[dict], emit: "Emit") -> None:
        """Run background reply-lint and emit `reply_lint` event when something fires.

        Non-blocking: never raises, never affects the reply path. Failures
        in the linter are silently swallowed — this is a defensive layer.
        """
        try:
            from src.tools import reply_check
            result = reply_check.self_check(text, recent_meta_flags=meta_flags)
            if not result.get("ok") and result.get("warnings"):
                await emit({
                    "type": "reply_lint",
                    "warnings": result["warnings"],
                    "summary": (
                        f"{result['_meta']['warning_count']} warning(s): "
                        f"{', '.join(result['_meta']['kinds'])}"
                    ),
                })
        except Exception:
            pass

    @staticmethod
    def _preview(content) -> str:
        if isinstance(content, str):
            return content[:500]
        try:
            return json.dumps(content, default=str)[:500]
        except Exception:
            return repr(content)[:500]

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.__aexit__(None, None, None)
            finally:
                self._client = None


def _extract_meta_from_tool_result(content) -> dict | None:
    """Best-effort parse of a ToolResultBlock's content to pull out `_meta`.

    Tool results are JSON-serialized in `_wrap_for_sdk` (registry.py), so
    when this content is a string or list-of-text-blocks, we can json-decode
    and find the `_meta` key. Returns None on any failure (silent).
    """
    # Normalize to a single text string
    text: str | None = None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        # SDK uses [{"type": "text", "text": "..."}, ...]
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif isinstance(item, str):
                parts.append(item)
        text = "".join(parts) if parts else None
    if not text:
        return None
    # The text may have a "⚠️ META: ...\n\n" prefix from registry's
    # _meta_warning_prefix — strip everything before the first '{'
    brace = text.find("{")
    if brace < 0:
        return None
    try:
        parsed = json.loads(text[brace:])
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    meta = parsed.get("_meta")
    return meta if isinstance(meta, dict) else None
