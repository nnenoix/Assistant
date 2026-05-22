# Workspace Agent — Tools Inventory

Auto-generated from `src/tools/registry.py`. **403 tools** across **72 categories**.

Each tool is exposed to Claude as `mcp__gworkagent__<name>`. Tools marked with an `account` param accept a Google account alias (`main` by default); some Drive tools also accept `"*"` or a list of aliases for multi-account fan-out.

## Categories

- [Aliases (local name → account)](#aliases) — 4 tools
- [Analytics](#analytics) — 2 tools
- [Approval](#approval) — 4 tools
- [Apps Script](#apps) — 28 tools
- [Audit](#audit) — 2 tools
- [Auth / Accounts](#auth) — 6 tools
- [Avito](#avito) — 7 tools
- [Bank statement parsers](#bank) — 3 tools
- [Bi](#bi) — 3 tools
- [Boxberry](#boxberry) — 6 tools
- [Browser automation (Playwright)](#browser) — 5 tools
- [Bulk payloads](#bulk) — 1 tools
- [Google Calendar](#calendar) — 12 tools
- [Cdek](#cdek) — 5 tools
- [Chat history](#chats) — 4 tools
- [Cloud Logging](#cloud) — 2 tools
- [Google Contacts (People API)](#contacts) — 5 tools
- [Cosine](#cosine) — 1 tools
- [Dadata](#dadata) — 5 tools
- [Diadoc](#diadoc) — 4 tools
- [Google Docs](#docs) — 6 tools
- [Google Drive](#drive) — 25 tools
- [Duckdb](#duckdb) — 5 tools
- [Embed](#embed) — 1 tools
- [Excel (.xlsx local)](#excel) — 1 tools
- [File analyze / extract](#files) — 5 tools
- [Google Forms](#forms) — 4 tools
- [Currency / FX](#fx) — 1 tools
- [GCP project management](#gcp) — 4 tools
- [Gmail](#gmail) — 17 tools
- [Imap](#imap) — 2 tools
- [Local filesystem](#local) — 6 tools
- [Lock](#lock) — 3 tools
- [Mdm](#mdm) — 4 tools
- [Moysklad](#moysklad) — 14 tools
- [Nlp](#nlp) — 5 tools
- [Agent notes (persistent memory)](#notes) — 5 tools
- [Notify](#notify) — 2 tools
- [Ocr](#ocr) — 2 tools
- [Onec](#onec) — 5 tools
- [Open external app](#open) — 1 tools
- [Ozon](#ozon) — 12 tools
- [Pandera](#pandera) — 1 tools
- [PDF generation](#pdf) — 1 tools
- [Pochta](#pochta) — 5 tools
- [Reply lint](#reply) — 1 tools
- [Reports](#report) — 7 tools
- [Sbis](#sbis) — 4 tools
- [Scheduler](#scheduler) — 4 tools
- [Self-heal / introspection](#self) — 9 tools
- [Google Sheets](#sheets) — 46 tools
- [Skill](#skill) — 3 tools
- [Google Slides](#slides) — 7 tools
- [Smsc](#smsc) — 3 tools
- [Smsru](#smsru) — 3 tools
- [Google Tasks](#tasks) — 7 tools
- [Team](#team) — 1 tools
- [Tg](#tg) — 4 tools
- [Tinkoff](#tinkoff) — 4 tools
- [Trace](#trace) — 2 tools
- [Translation](#translate) — 2 tools
- [Tspl](#tspl) — 1 tools
- [Claim verification](#verify) — 1 tools
- [Vision (image analysis)](#vision) — 2 tools
- [Vk](#vk) — 6 tools
- [Drive watcher](#watcher) — 4 tools
- [Wildberries (WB)](#wb) — 15 tools
- [Web fetch](#web) — 2 tools
- [Webhook](#webhook) — 3 tools
- [Yamarket](#yamarket) — 9 tools
- [Yookassa](#yookassa) — 5 tools
- [Zpl](#zpl) — 2 tools

---

## aliases

_Aliases (local name → account)_ — 4 tools.

### `aliases_add`

_Policy op:_ `aliases.write`

Register a name→account binding or merge new info into an existing entry. Bind multiple names (including nicknames) to one account alias. Call proactively when the user introduces a new person.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | yes | OAuth alias (must already exist via auth_add_account). |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `email` | string | no |  |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `names` | string \| array<string> | yes |  |
| `note` | string | no |  |

### `aliases_list`

_Policy op:_ `aliases.read`

List all entries in the local alias registry. Each entry binds one or more human names (and optionally an email) to a Google account alias. Distinct from Google Contacts (contacts_*).

_No parameters._

### `aliases_remove`

_Policy op:_ `aliases.write`

Drop an alias binding by account.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | yes |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |

### `aliases_resolve`

_Policy op:_ `aliases.read`

Resolve free-text ('Лена', 'партнёр', email) → registry entries. Call FIRST when user mentions a person by name. One hit → use .account. Multiple → ask to disambiguate. Zero → ask + aliases_add.

| Param | Type | Required | Description |
|---|---|---|---|
| `hint` | string | yes |  |

---

## analytics

_Analytics_ — 2 tools.

### `analytics_abc`

_Policy op:_ `local.read`

ABC analysis (80/15/5) on row dicts. Groups by sku_col, sums revenue/qty/profit, computes ABC class per metric, composite code ('AAA'=leader, 'CCC'=cut). Returns {total_skus, total_revenue, categories, abc_rev_counts, top_a, rows}. Optional `costs`=[{sku, cost}] → final_profit = revenue − cost×qty.

| Param | Type | Required | Description |
|---|---|---|---|
| `costs` | array<?> | no | Optional [{sku, cost}] — purchase cost per SKU for final_profit calculation. |
| `profit_col` | string | no |  |
| `qty_col` | string | no |  |
| `revenue_col` | string | no |  |
| `rows` | array<?> | yes | List of row dicts (e.g. from sheets_query or excel_parse). Must have sku, revenue, qty columns. |
| `sku_col` | string | no |  |

### `analytics_abc_split`

_Policy op:_ `local.read`

Quick 1-metric ABC classification on rows. Sorts rows by `metric` desc, cumsum, assigns A (≤80%), B (≤95%), C (rest). Returns rows with new `abc` key. Use when you only need ABC on ONE metric (vs analytics_abc which does 3-metric composite).

| Param | Type | Required | Description |
|---|---|---|---|
| `metric` | string | yes |  |
| `rows` | array<?> | yes |  |

---

## approval

_Approval_ — 4 tools.

### `approval_decide`

_Policy op:_ `local.write`

Approve or deny a pending request. status: approved | denied.

| Param | Type | Required | Description |
|---|---|---|---|
| `approval_id` | string | yes |  |
| `decided_by` | string | no |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `note` | string | no |  |
| `status` | string | yes |  |

### `approval_list`

_Policy op:_ `local.read`

List recent approvals. status: pending | approved | denied | any.

| Param | Type | Required | Description |
|---|---|---|---|
| `limit` | integer | no |  |
| `status` | string | no |  |

### `approval_request`

_Policy op:_ `local.write`

Stage an approval request for a destructive action. Returns {approval_id}. Caller polls approval_status; when 'approved', the destructive op may run.

| Param | Type | Required | Description |
|---|---|---|---|
| `action` | string | yes |  |
| `args` | object | yes |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `reason` | string | no |  |
| `requested_by` | string | no |  |

### `approval_status`

_Policy op:_ `local.read`

Latest status of an approval (returns the most recent decision row).

| Param | Type | Required | Description |
|---|---|---|---|
| `approval_id` | string | yes |  |

---

## apps

_Apps Script_ — 28 tools.

### `apps_script_api_create_deployment`

_Policy op:_ `apps_script.edit`

Create an API-executable deployment of the script pinned to a version_number. Needed for apps_script_api_run_function with dev_mode=False (pinned code). For testing latest code, use dev_mode=True and skip deployment entirely.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `description` | string | no |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `script_id` | string | yes |  |
| `version_number` | integer | yes |  |

### `apps_script_api_create_project`

_Policy op:_ `apps_script.edit`

Create a fresh standalone Apps Script project owned by `account`. Returns {scriptId, title, ...}. Use this for ad-hoc test/runner scripts — then push files via apps_script_api_edit_file. Set parent_id to bind the script to a Drive folder/spreadsheet.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `parent_id` | string | no | Optional Drive ID — if set, script is bound to it (e.g. a spreadsheet). |
| `title` | string | yes |  |

### `apps_script_api_create_version`

_Policy op:_ `apps_script.edit`

Create a new VERSION of an Apps Script project — required for libraries: consumer scripts pin a versionNumber, code changes only become visible to them after a new version is created. Returns {scriptId, versionNumber, createTime, description}.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `description` | string | no | Free-text changelog for this version, shown in the script editor's Version manager. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `script_id` | string | yes |  |

### `apps_script_api_edit_file`

_Policy op:_ `apps_script.edit`

Replace ONE file's WHOLE source (add if missing), preserving other files. For surgical fixes to ONE function in a multi-function file prefer apps_script_api_replace_function — safer.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `file_name` | string | yes | Without extension. E.g. '2.3 Финансы с датой отчета' (not '...js'). |
| `file_type` | string | no | SERVER_JS (default) / JSON / HTML. |
| `new_source` | string | yes |  |
| `script_id` | string | yes |  |

### `apps_script_api_find_bound_script`

_Policy op:_ `apps_script.edit`

Brute-force find bound script(s) for a spreadsheet — Drive search by mime='script' misses bound scripts. Enumerates every visible script, calls projects.get, filters by parentId. Slow (~1s/script). Returns [{script_id, title}].

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `spreadsheet_id` | string | yes |  |

### `apps_script_api_get_bound_script_token`

_Policy op:_ `apps_script.edit`

Extract an API token (e.g. WB) from the bound script of `spreadsheet_id`. Convention: `function getToken() { return "<token>"; }`. Returns {token, script_id, file_name, function_name}. Auto-resolves bound script; on miss tells you to register via apps_script_api_register_bound_script.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `function_name` | string | no |  |
| `spreadsheet_id` | string | yes |  |

### `apps_script_api_get_content`

_Policy op:_ `apps_script.edit`

Read FULL source (all files inline) of an Apps Script project. Often >100k chars → gets truncated. Prefer apps_script_api_list_files + apps_script_api_get_file (staged to disk). Use only when you need everything in memory.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `script_id` | string | yes |  |

### `apps_script_api_get_file`

_Policy op:_ `apps_script.edit`

Fetch ONE file, STAGE locally to `.data/staging/<script_id>/<file_name>.gs`. Returns staged_path + preview. Read staged via local_read_file (offset/limit for big files), edit, push back via apps_script_api_edit_file. Canonical local-first read path.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `file_name` | string | yes | Without extension. E.g. '2.3 Финансы с датой отчета'. |
| `script_id` | string | yes |  |

### `apps_script_api_get_project`

_Policy op:_ `apps_script.edit`

Project metadata: title, parentId (spreadsheet for bound scripts), owner, createTime.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `script_id` | string | yes |  |

### `apps_script_api_list_bound_scripts`

_Policy op:_ `apps_script.edit`

List all spreadsheet→script mappings the agent has learned. Use to check if a spreadsheet is already registered before asking the user for the script_id.

| Param | Type | Required | Description |
|---|---|---|---|
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |

### `apps_script_api_list_files`

_Policy op:_ `apps_script.edit`

List file names + types + sizes (lines, bytes) of an Apps Script project. NO source content — won't blow the token cap. Use this FIRST to see what's in the project, then fetch the specific file you care about with apps_script_api_get_file.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `script_id` | string | yes |  |

### `apps_script_api_list_versions`

_Policy op:_ `apps_script.edit`

List all versions of an Apps Script project. Useful before creating a new version (to know the next number) or to diagnose 'which version does the consumer pin?'.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `script_id` | string | yes |  |

### `apps_script_api_register_bound_script`

_Policy op:_ `apps_script.edit`

Record which script_id is bound to a spreadsheet (Drive API doesn't enumerate bound scripts). Get script_id from `script.google.com/d/<SCRIPT_ID>/edit`. After registration, get_bound_script_token + resolve_bound_script work instantly.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `notes` | string | no | Optional human description of what this script does. |
| `script_id` | string | yes |  |
| `spreadsheet_id` | string | yes |  |

### `apps_script_api_replace_function`

_Policy op:_ `apps_script.edit`

Surgical: replace EXACTLY one function, preserving everything else (comments, whitespace, other functions). Walks JS braces to find span. Prefer over edit_file in multi-function files — eliminates the risk of dropping other functions when the source was truncated.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `file_name` | string | yes |  |
| `function_name` | string | yes |  |
| `new_source` | string | yes | Full text of the new function, starting with 'function NAME(...)' and ending with the closing '}'. |
| `script_id` | string | yes |  |

### `apps_script_api_resolve_bound_script`

_Policy op:_ `apps_script.edit`

Resolve `spreadsheet_id` → its bound Apps Script ID. Tries: local registry → Drive enum → Playwright browser (Extensions→Apps Script). Successful discoveries cached. Returns {script_id, source, account}.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `spreadsheet_id` | string | yes |  |
| `use_browser` | boolean | no | Whether to fall back to Playwright if registry/enumeration fail. Disable for tests. |

### `apps_script_api_run_ad_hoc`

_Policy op:_ `apps_script.run`

ONE-SHOT: create temp script, push code, run, return result, delete. Best for ad-hoc 'what does this return'. Manifest auto-built with executionApi.access=MYSELF. If library_id+library_version set, library wired as `library_symbol` (default 'Mylib'). keep_project=True retains for inspection.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `code` | string | yes | Full Apps Script source — must define `function_name`. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `function_name` | string | no |  |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `keep_project` | boolean | no |  |
| `library_id` | string | no | Optional. Apps Script library script ID to attach as dependency. |
| `library_symbol` | string | no | Symbol the library is exposed as inside the script. |
| `library_version` | integer | no | Library version pinned in manifest. |
| `params` | array<?> | no | Positional args passed to the function. |

### `apps_script_api_run_function`

_Policy op:_ `apps_script.run`

Run a function via Apps Script API. Returns {ok, result | error_type/message/stack}. Script's appsscript.json needs `executionApi.access` ("MYSELF"). Args via `params` (JSON-serializable list). dev_mode=True runs HEAD (testing); False runs the pinned API-exec deployment.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dev_mode` | boolean | no | True = run HEAD code, False = run pinned API-exec deployment. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `function_name` | string | yes |  |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `params` | array<?> | no | Positional arguments. JSON-serializable values. Empty/omitted for no-arg functions. |
| `script_id` | string | yes |  |

### `apps_script_api_run_smart`

_Policy op:_ `apps_script.run`

Cascade run: tries scripts.run dev → scripts.run pinned → Playwright custom-menu click. Use when the script is bound to a spreadsheet whose GCP project might not match ours. Pass custom_menu_path (e.g. ['☰ WB', 'API', 'Фин.отчеты']) to enable the menu fallback.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `custom_menu_path` | array<string> | no |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `function_name` | string | no |  |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `params` | array<?> | no |  |
| `script_id` | string | no |  |
| `spreadsheet_id` | string | no | For Playwright menu fallback |
| `wait_after_menu_sec` | integer | no |  |

### `apps_script_api_status`

_Policy op:_ `apps_script.read`

Health check for the Apps Script API on `account`. Verifies (a) OAuth token has script.projects/deployments/scriptapp scopes and (b) the API is reachable (via projects.get on `script_id`, or the Phase 14 aggregator if configured). Use BEFORE apps_script_api_run_ad_hoc when in doubt — saves a wasted project-create on a doomed call. Returns {ok, scopes:{required,granted,missing}, api_reachable, api_error?, api_meta?, aggregator?}.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `script_id` | string | no | Optional script ID to ping with projects.get. If omitted, falls back to the Phase 14 aggregator; if neither, returns scope-only (api_reachable=null). |

### `apps_script_api_triggers_install_one_shot`

_Policy op:_ `apps_script.run`

Install a one-shot CLOCK trigger that fires `function_name` after `delay_minutes`. Useful for scheduling work that must run later (e.g. retry after a WB rate-limit window). Requires GCP project alignment (use browser_set_script_gcp_project if needed).

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `delay_minutes` | integer | no |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `function_name` | string | yes |  |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `script_id` | string | yes |  |

### `apps_script_api_triggers_list`

_Policy op:_ `apps_script.run`

List installed triggers on a script. Returns [{id, function, event_type, source}].

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `script_id` | string | yes |  |

### `apps_script_api_triggers_remove`

_Policy op:_ `apps_script.edit`

Remove triggers by ID or handler function name. Returns {removed_count}.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `function_name` | string | no |  |
| `script_id` | string | yes |  |
| `trigger_id` | string | no |  |

### `apps_script_api_update_content`

_Policy op:_ `apps_script.edit`

Replace the FULL file set of an Apps Script project. Prefer apps_script_api_edit_file for single-file fixes; use this only when modifying multiple files at once. `files` is the complete list of {name, type, source}; any file you omit will be DELETED from the project.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `files` | array<object> | yes |  |
| `script_id` | string | yes |  |

### `apps_script_api_update_library_dependency`

_Policy op:_ `apps_script.edit`

In consumer `consumer_script_id`, point a library dependency at `new_version`. Adds the entry if missing. Final step of library deploy: (1) edit library file → (2) create_version → (3) update_library_dependency on each consumer → (4) consumer's next call sees fixed code.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `consumer_script_id` | string | yes |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `library_script_id` | string | yes |  |
| `new_version` | integer | yes |  |
| `user_symbol` | string | no | Optional. The alias the library is exposed as (e.g. 'Mylib'). Leave empty to preserve existing. |

### `apps_script_clone`

_Policy op:_ `apps_script.edit`

Clone (or pull) an Apps Script project to local .data/scripts/.

| Param | Type | Required | Description |
|---|---|---|---|
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `script_id` | string | yes |  |

### `apps_script_oneshot`

_Policy op:_ `apps_script.run`

Run a one-off Apps Script: creates standalone script, pushes code, runs via clasp, returns result. For tasks too complex for QUERY/find_replace/iter_rows — full SpreadsheetApp/Drive API access, can read+mutate many files. First call may fail 'not deployed' (one-time per-script setup); response then has script_url for manual deploy.

| Param | Type | Required | Description |
|---|---|---|---|
| `alias` | string | no | Optional alias for the project — useful when keep_project=true so you can re-run via apps_script_run. |
| `code` | string | yes | Full Apps Script source, must define a function named `function_name` (default 'main') with no required arguments. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `function_name` | string | no | Default 'main'. |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `keep_project` | boolean | no | Default false. Set true to preserve the local clone for re-runs. |

### `apps_script_push`

_Policy op:_ `apps_script.edit`

Push local clasp-cloned project changes to Google. Only needed when you've used apps_script_clone for legacy clasp-based projects; for normal edits prefer apps_script_api_edit_file (pushes directly).

| Param | Type | Required | Description |
|---|---|---|---|
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `script_id` | string | yes |  |

### `apps_script_run`

_Policy op:_ `apps_script.run`

Run a function in an Apps Script that has been deployed as API executable.

| Param | Type | Required | Description |
|---|---|---|---|
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `function_name` | string | yes |  |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `params` | array<?> | no |  |
| `script_id` | string | yes |  |

---

## audit

_Audit_ — 2 tools.

### `audit_log`

_Policy op:_ `local.write`

Append a row to the local audit log. Tools should call this just before/after every destructive action with the args summary.

| Param | Type | Required | Description |
|---|---|---|---|
| `action` | string | yes |  |
| `actor` | string | no |  |
| `args` | object | yes |  |
| `correlation_id` | string | no |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `result_summary` | string | no |  |
| `tool` | string | yes |  |

### `audit_search`

_Policy op:_ `local.read`

Search audit log by actor / tool / action / since timestamp. Latest-first.

| Param | Type | Required | Description |
|---|---|---|---|
| `action` | string | no |  |
| `actor` | string | no |  |
| `limit` | integer | no |  |
| `since_iso` | string | no |  |
| `tool` | string | no |  |

---

## auth

_Auth / Accounts_ — 6 tools.

### `auth_add_account`

_Policy op:_ `auth.add`

Authorize a new Google account under the given alias. Opens a browser on this machine; the user must log in and grant permissions. Blocks until the OAuth flow completes (~30s).

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | yes | Short alias for the new account, e.g. 'work', 'partner', or an email. |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |

### `auth_add_account_incremental`

_Policy op:_ `auth.add`

Re-authorize an account adding NEW scopes while preserving existing grants (Google's incremental authorization with include_granted_scopes=true). Cleaner than delete+re-add — the user only sees the new scopes in the consent screen.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | yes |  |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `new_scopes` | array<string> | no | Scope URLs to add. |

### `auth_describe_account`

_Policy op:_ `auth.list`

Identify which Google account is bound to a token alias. Returns {email, name, scopes}. Use after auth_add_account to verify the consent screen picked the right account (we've been burned by accidentally picking the wrong one).

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no |  |

### `auth_list_accounts`

_Policy op:_ `auth.list`

List configured OAuth account aliases. Each alias corresponds to a Google account whose Drive/Sheets the agent can read and edit.

_No parameters._

### `auth_list_accounts_with_identity`

_Policy op:_ `auth.list`

Like auth_list_accounts but also fetches each alias's bound email + name. One-stop 'who is what'.

_No parameters._

### `auth_remove_account`

_Policy op:_ `auth.remove`

Forget the stored token for the given account alias. Does NOT revoke the OAuth grant in the Google account itself.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | yes |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |

---

## avito

_Avito_ — 7 tools.

### `avito_auth`

_Policy op:_ `drive.read`

Avito OAuth2 client_credentials → access_token (~24h).

| Param | Type | Required | Description |
|---|---|---|---|
| `client_id` | string | yes |  |
| `client_secret` | string | yes |  |

### `avito_balance`

_Policy op:_ `drive.read`

Avito wallet balance.

| Param | Type | Required | Description |
|---|---|---|---|
| `token` | string | yes |  |
| `user_id` | integer | yes |  |

### `avito_messenger_chats`

_Policy op:_ `drive.read`

Avito messenger chats list.

| Param | Type | Required | Description |
|---|---|---|---|
| `limit` | integer | no |  |
| `offset` | integer | no |  |
| `token` | string | yes |  |
| `user_id` | integer | yes |  |

### `avito_messenger_messages`

_Policy op:_ `drive.read`

Avito messages in one chat.

| Param | Type | Required | Description |
|---|---|---|---|
| `chat_id` | string | yes |  |
| `limit` | integer | no |  |
| `offset` | integer | no |  |
| `token` | string | yes |  |
| `user_id` | integer | yes |  |

### `avito_self_info`

_Policy op:_ `drive.read`

Avito seller account info.

| Param | Type | Required | Description |
|---|---|---|---|
| `token` | string | yes |  |

### `avito_send_message`

_Policy op:_ `gmail.send`

Send Avito chat message.

| Param | Type | Required | Description |
|---|---|---|---|
| `chat_id` | string | yes |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `text` | string | yes |  |
| `token` | string | yes |  |
| `user_id` | integer | yes |  |

### `avito_user_items`

_Policy op:_ `drive.read`

Avito listings of a seller. status: active / removed / old / blocked / rejected.

| Param | Type | Required | Description |
|---|---|---|---|
| `page` | integer | no |  |
| `per_page` | integer | no |  |
| `status` | string | no |  |
| `token` | string | yes |  |
| `user_id` | integer | yes |  |

---

## bank

_Bank statement parsers_ — 3 tools.

### `bank_detect`

_Policy op:_ `local.read`

Quick check whether a file is a recognized bank statement. Returns {bank} (e.g. 'sber') or {bank: null, error: 'no parser matched'}. Cheap — runs each parser's can_parse() in order. Use before parse_statement to confirm format.

| Param | Type | Required | Description |
|---|---|---|---|
| `file_path` | string | yes |  |

### `bank_list_supported`

_Policy op:_ `local.read`

List the bank ids the parser supports.

_No parameters._

### `bank_parse_statement`

_Policy op:_ `local.read`

Parse a Russian bank statement PDF or 1С client-bank .txt. Auto-detects: Сбер, Альфа, Т-Банк, Газпром, ВТБ, Райф, ЮниКредит, Ozon, Modul, Точка, WB, 1С. Returns {bank, transactions: [{date, description, amount_cents, inn?, counterparty?}], account_last4?}. **amount_cents in КОПЕЙКАХ** (×0.01 для ₽).

| Param | Type | Required | Description |
|---|---|---|---|
| `bank_hint` | string | no | Optional. One of: alfa, sber, sber_business, tinkoff, gazprom, vtb, raif, unicredit, ozon, modul, tochka, wb_bank, clientbank_1c. |
| `file_path` | string | yes |  |

---

## bi

_Bi_ — 3 tools.

### `bi_dashboard_render`

_Policy op:_ `local.write`

Render a one-page self-contained HTML dashboard. kpis = [{label, value, delta?, unit?}].

| Param | Type | Required | Description |
|---|---|---|---|
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `html_path` | string | yes |  |
| `kpis` | array<object> | yes |  |
| `title` | string | yes |  |

### `bi_kpi_history_get`

_Policy op:_ `local.read`

Recent KPI history points for a named series.

| Param | Type | Required | Description |
|---|---|---|---|
| `limit` | integer | no |  |
| `name` | string | yes |  |

### `bi_kpi_history_log`

_Policy op:_ `local.write`

Append a KPI value to local history for trend charts.

| Param | Type | Required | Description |
|---|---|---|---|
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `name` | string | yes |  |
| `tags` | object | no |  |
| `ts` | string | no |  |
| `value` | number | yes |  |

---

## boxberry

_Boxberry_ — 6 tools.

### `boxberry_courier_list_cities`

_Policy op:_ `drive.read`

Cities where Boxberry courier pickup is available.

| Param | Type | Required | Description |
|---|---|---|---|
| `token` | string | yes |  |

### `boxberry_list_parcels`

_Policy op:_ `drive.read`

List uploaded Boxberry parcels. `from_id` is the resume-cursor (parcel id).

| Param | Type | Required | Description |
|---|---|---|---|
| `from_id` | string | no |  |
| `token` | string | yes |  |

### `boxberry_list_points`

_Policy op:_ `drive.read`

List Boxberry pickup points. `city_code` optional filter.

| Param | Type | Required | Description |
|---|---|---|---|
| `city_code` | string | no |  |
| `token` | string | yes |  |

### `boxberry_list_services`

_Policy op:_ `drive.read`

Cost breakdown (delivery, insurance, ...) for one Boxberry parcel.

| Param | Type | Required | Description |
|---|---|---|---|
| `im_id` | string | yes |  |
| `token` | string | yes |  |

### `boxberry_list_statuses`

_Policy op:_ `drive.read`

Status history for one Boxberry parcel.

| Param | Type | Required | Description |
|---|---|---|---|
| `im_id` | string | yes |  |
| `token` | string | yes |  |

### `boxberry_parcel_check`

_Policy op:_ `drive.read`

Verify one Boxberry parcel by your internal id.

| Param | Type | Required | Description |
|---|---|---|---|
| `im_id` | string | yes |  |
| `token` | string | yes |  |

---

## browser

_Browser automation (Playwright)_ — 5 tools.

### `browser_click_custom_menu`

_Policy op:_ `apps_script.run`

Open sheet in browser, click through a custom menu chain (e.g. ['☰ WB', 'API', 'Фин.отчеты']) to trigger a bound-script function. Use when scripts.run fails with 403/404 (bound script in Google's default GCP project). Snapshot affected range before/after to verify.

| Param | Type | Required | Description |
|---|---|---|---|
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `headless` | boolean | no |  |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `menu_path` | array<string> | yes | Visible text of each menu item top-down. Substring matches OK (e.g. 'WB' matches '☰ WB'). |
| `spreadsheet_id` | string | yes |  |
| `timeout_sec` | integer | no |  |
| `wait_after_click_sec` | integer | no | Hold the browser tab open this many seconds after the final click so the function can finish (max Apps Script runtime is 6 min). |

### `browser_get_bound_script_id`

_Policy op:_ `apps_script.edit`

Open a sheet in Chromium, click Extensions → Apps Script, capture the bound script_id from the new tab's URL. Only reliable way — APIs won't enumerate bound scripts. First call needs `headless=False` for Google login (profile cached in `.data/browser_profile/`). Usually called via apps_script_api_resolve_bound_script which caches results.

| Param | Type | Required | Description |
|---|---|---|---|
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `headless` | boolean | no | False shows a visible browser window — needed for first-time login. True runs invisibly after the profile is logged in. |
| `spreadsheet_id` | string | yes |  |
| `timeout_sec` | integer | no |  |

### `browser_list_profiles`

_Policy op:_ `apps_script.edit`

List browser profiles configured. Each profile is an independent persistent Chromium profile, allowing different Google accounts to be logged in for different sessions.

| Param | Type | Required | Description |
|---|---|---|---|
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |

### `browser_login_interactive`

_Policy op:_ `apps_script.edit`

Open visible Chromium at Google login. User logs in once; profile cached in `.data/browser_profile/`. Run BEFORE browser_get_bound_script_id with headless=True, or whenever session expires.

| Param | Type | Required | Description |
|---|---|---|---|
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `timeout_sec` | integer | no |  |

### `browser_set_script_gcp_project`

_Policy op:_ `apps_script.edit`

Switch an Apps Script project's GCP project to `project_number` (e.g. 148389149001 — our OAuth client). Needed for scripts.run / Cloud Logging / triggers on bound scripts. Playwright clicks Project Settings → Change project.

| Param | Type | Required | Description |
|---|---|---|---|
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `headless` | boolean | no | Visible first time so you can confirm the change. |
| `profile` | string | no |  |
| `project_number` | string | yes |  |
| `script_id` | string | yes |  |

---

## bulk

_Bulk payloads_ — 1 tools.

### `bulk_load_results`

_Policy op:_ `sheets.read`

Drill down to full per-item data from a previous bulk tool result. Paginated — default limit=150 entries/page fits MAX_TOOL_PAYLOAD. For 500-book results, call with offset=0, then 150, 300, 450 until `_meta.has_more=false`. Pass the `_meta.result_token` returned by `sheets_bulk_metric` / `sheets_bulk_read`. Returns {items, errors, op, _meta:{offset, page_size, total, has_more, next_offset}}. Tokens expire after ~100 most-recent bulk results.

| Param | Type | Required | Description |
|---|---|---|---|
| `limit` | integer | no | Max items per page. Default 150 (≈10KB JSON for typical Drive IDs). |
| `offset` | integer | no | 0-based start index. Default 0. |
| `result_token` | string | yes | Token from a prior bulk tool's _meta.result_token. |

---

## calendar

_Google Calendar_ — 12 tools.

### `calendar_create_event`

_Policy op:_ `calendar.write`

Create event. start/end: 'YYYY-MM-DD' (all-day) or 'YYYY-MM-DD HH:MM' (timed in `timezone_str`); end defaults to start+1h. reminder_minutes popup, None=no reminder. `recurrence` accepts RFC5545 RRULEs e.g. ['RRULE:FREQ=WEEKLY;BYDAY=MO,WE;COUNT=10'].

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `attendees` | array<string> | no | List of email addresses. |
| `calendar_id` | string | no | Default 'primary'. |
| `description` | string | no |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `end` | string | no | Optional. Same format as start. |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `location` | string | no |  |
| `recurrence` | array<string> | no | RFC5545 RRULE strings for repeating events. |
| `reminder_minutes` | integer | no | Minutes before event to popup (default 15). 0 = at start. null = no reminder. |
| `start` | string | yes | 'YYYY-MM-DD' (all-day) or 'YYYY-MM-DD HH:MM' (timed). |
| `summary` | string | yes |  |
| `timezone_str` | string | no | Default 'Europe/Moscow'. |

### `calendar_delete_event`

_Policy op:_ `calendar.delete`

Delete an event.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `calendar_id` | string | no |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `event_id` | string | yes |  |

### `calendar_find_free_time`

_Policy op:_ `calendar.read`

Find free slots of `duration_minutes` between work_hours_start..work_hours_end across a date range. Uses Calendar's free/busy. Returns up to 20 earliest slots. Use for 'когда у меня свободно' / 'найди время на встречу с Х'.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `calendar_id` | string | no |  |
| `duration_minutes` | integer | no | Default 60. |
| `end_date` | string | yes | 'YYYY-MM-DD' (inclusive). |
| `start_date` | string | yes | 'YYYY-MM-DD'. |
| `timezone_str` | string | no |  |
| `weekdays_only` | boolean | no | Default true (skip Sat/Sun). |
| `work_hours_end` | integer | no | Default 19. |
| `work_hours_start` | integer | no | Default 9. |

### `calendar_find_meeting_slot`

_Policy op:_ `calendar.read`

Find the FIRST common free slot of `duration_minutes` for all `attendees` in [time_min, time_max]. Defaults to weekdays 09:00-19:00 in the calendar's local time. Returns {found, slot, candidates_checked}.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `attendees` | array<string> | yes |  |
| `duration_minutes` | integer | yes |  |
| `time_max` | string | yes |  |
| `time_min` | string | yes |  |
| `weekdays_only` | boolean | no |  |
| `work_hours_end` | integer | no |  |
| `work_hours_start` | integer | no |  |
| `working_hours_only` | boolean | no |  |

### `calendar_freebusy`

_Policy op:_ `calendar.read`

Query free/busy slots across one or more calendars (by email). Returns {per_email: [{email, busy: [{start, end}], errors: []}], _meta:{time_min, time_max}}. Use for 'когда у X занято на этой неделе'.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `emails` | array<string> | yes |  |
| `time_max` | string | yes |  |
| `time_min` | string | yes | YYYY-MM-DD or RFC3339. |

### `calendar_get_event`

_Policy op:_ `calendar.read`

Full details of one event by id.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `calendar_id` | string | no |  |
| `event_id` | string | yes |  |

### `calendar_list_calendars`

_Policy op:_ `calendar.read`

List all calendars the account has access to. Identifies the 'primary' one.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |

### `calendar_list_events`

_Policy op:_ `calendar.read`

Events in a date range. time_min/time_max: 'YYYY-MM-DD' or RFC3339. If both omitted → defaults to today+7d, flagged via `_meta.window.default_used=true`. Returns {events, _meta:{window, truncated}}. ALWAYS surface the scanned window before saying 'нет встреч' / 'свободно'.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `calendar_id` | string | no | Default 'primary'. |
| `max_results` | integer | no | Default 50, max 250. |
| `query` | string | no | Optional free-text filter. |
| `time_max` | string | no | Default: +7 days. |
| `time_min` | string | no | Default: now. Format 'YYYY-MM-DD' or RFC3339. |

### `calendar_list_recurring_instances`

_Policy op:_ `calendar.read`

Expand a recurring event into its concrete instances within a window. Use after creating a recurring event to confirm when its repetitions fall.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `calendar_id` | string | no |  |
| `event_id` | string | yes |  |
| `time_max` | string | yes |  |
| `time_min` | string | yes |  |

### `calendar_overlay_accounts`

_Policy op:_ `calendar.read`

Cross-account FreeBusy: pass {account_alias: [emails]} and get a unified busy/free map across multiple configured Google accounts. Useful when consolidating availability across the user's personal + work accounts.

| Param | Type | Required | Description |
|---|---|---|---|
| `emails_per_account` | object | yes |  |
| `time_max` | string | yes |  |
| `time_min` | string | yes |  |

### `calendar_quick_reminder`

_Policy op:_ `calendar.write`

Shortcut for 'напомни мне когда': creates a brief event at `when` with a popup reminder. Use for simple reminders like 'напомни мне в среду в 15:00 проверить ВБ-отчёт'. reminder_minutes=0 → popup at event start.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `reminder_minutes` | integer | no | Default 0 = popup at event start. |
| `text` | string | yes | What to remind about (becomes event title). |
| `when` | string | yes | 'YYYY-MM-DD HH:MM' or 'YYYY-MM-DD'. |

### `calendar_update_event`

_Policy op:_ `calendar.write`

Patch fields on an existing event. `updates` is a dict with any of: summary, description, location, start, end, attendees, reminders, status. For time fields use {date} or {dateTime, timeZone}.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `calendar_id` | string | no |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `event_id` | string | yes |  |
| `updates` | object | yes |  |

---

## cdek

_Cdek_ — 5 tools.

### `cdek_auth`

_Policy op:_ `drive.read`

Get SDEK OAuth2 access token (lifetime 1h). Pass result.data.access_token as `token` to other cdek_* tools.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | yes |  |
| `secret` | string | yes |  |

### `cdek_calculator`

_Policy op:_ `drive.read`

SDEK cost calculator. `from_code`/`to_code` are SDEK location codes (use cdek_locations_search). `tariff_code` 136 = склад-склад.

| Param | Type | Required | Description |
|---|---|---|---|
| `from_code` | integer | yes |  |
| `height_cm` | integer | no |  |
| `length_cm` | integer | no |  |
| `tariff_code` | integer | no |  |
| `to_code` | integer | yes |  |
| `token` | string | yes |  |
| `weight_g` | integer | no |  |
| `width_cm` | integer | no |  |

### `cdek_locations_search`

_Policy op:_ `drive.read`

Search SDEK location codes by city name. Use the returned code in other endpoints.

| Param | Type | Required | Description |
|---|---|---|---|
| `country_code` | string | no |  |
| `query` | string | yes |  |
| `size` | integer | no |  |
| `token` | string | yes |  |

### `cdek_order_get`

_Policy op:_ `drive.read`

Single SDEK shipment by UUID.

| Param | Type | Required | Description |
|---|---|---|---|
| `token` | string | yes |  |
| `uuid` | string | yes |  |

### `cdek_orders_list`

_Policy op:_ `drive.read`

List SDEK shipments. Dates ISO8601. Paginated by limit+offset.

| Param | Type | Required | Description |
|---|---|---|---|
| `date_from` | string | no |  |
| `date_to` | string | no |  |
| `limit` | integer | no |  |
| `offset` | integer | no |  |
| `token` | string | yes |  |

---

## chats

_Chat history_ — 4 tools.

### `chats_list_recent`

_Policy op:_ `chats.read`

List recent saved chat sessions, newest first. Each entry has id, title (taken from the first user message), started_at, message_count. Use to remind the user (or yourself) what was discussed recently.

| Param | Type | Required | Description |
|---|---|---|---|
| `limit` | integer | no | Default 30. |

### `chats_read`

_Policy op:_ `chats.read`

Read the full transcript of a specific past chat by id. The id format is a timestamp like '2026-05-16T14-30-00'.

| Param | Type | Required | Description |
|---|---|---|---|
| `chat_id` | string | yes |  |

### `chats_search`

_Policy op:_ `chats.read`

Substring search across ALL saved chats. Returns matches with short snippets so you can decide which chat to read in full. Use when the user references prior work ('что мы делали с таблицей X на прошлой неделе').

| Param | Type | Required | Description |
|---|---|---|---|
| `limit` | integer | no |  |
| `query` | string | yes |  |

### `chats_search_semantic`

_Policy op:_ `chats.read`

Semantic search across saved chats (local embeddings). Better than chats_search for fuzzy queries ('налоги' matches 'НДС'). Falls back to substring if embedding model unavailable; `_meta.search_method` flags which.

| Param | Type | Required | Description |
|---|---|---|---|
| `query` | string | yes |  |
| `top_k` | integer | no |  |

---

## cloud

_Cloud Logging_ — 2 tools.

### `cloud_logging_read`

_Policy op:_ `apps_script.edit`

Read recent Cloud Logging entries with an optional advanced filter. Use this to fetch Apps Script Logger.log output without scraping the editor UI. Common filter: 'resource.type="app_script_function" AND resource.labels.script_id="<id>"'. Defaults to last 60 minutes.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `filter_expr` | string | no | Cloud Logging filter; omit for all entries. |
| `minutes_back` | integer | no |  |
| `page_size` | integer | no |  |
| `project_id` | string | no |  |

### `cloud_logging_script_executions`

_Policy op:_ `apps_script.edit`

List recent function executions for a specific Apps Script. Returns one entry per execution_id with status + start time + log count. Requires the script to be linked to our GCP project (use browser_set_script_gcp_project first).

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `minutes_back` | integer | no |  |
| `project_id` | string | no |  |
| `script_id` | string | yes |  |

---

## contacts

_Google Contacts (People API)_ — 5 tools.

### `contacts_create`

_Policy op:_ `contacts.write`

Create a new Google Contact. Requires the `contacts` (write) scope.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `emails` | array<string> | no |  |
| `family_name` | string | no |  |
| `given_name` | string | yes |  |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `notes` | string | no |  |
| `organization` | string | no |  |
| `phones` | array<string> | no |  |

### `contacts_delete`

_Policy op:_ `contacts.write`

Permanently delete a contact by resource_name.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `resource_name` | string | yes |  |

### `contacts_get`

_Policy op:_ `contacts.read`

Get full details for one contact by resource_name (e.g. 'people/c12345').

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `resource_name` | string | yes |  |

### `contacts_list_all`

_Policy op:_ `contacts.read`

List all contacts (capped at max_results, up to 1000). `_meta.truncated=true` if there are more.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `max_results` | integer | no |  |

### `contacts_search`

_Policy op:_ `contacts.read`

Search user's Google Contacts. Returns flattened contact dicts with display_name, emails, phones, organizations.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `max_results` | integer | no |  |
| `query` | string | yes |  |

---

## cosine

_Cosine_ — 1 tools.

### `cosine_similarity`

_Policy op:_ `local.read`

Cosine similarity between two equal-length vectors.

| Param | Type | Required | Description |
|---|---|---|---|
| `a` | array<number> | yes |  |
| `b` | array<number> | yes |  |

---

## dadata

_Dadata_ — 5 tools.

### `dadata_clean_address`

_Policy op:_ `drive.read`

DaData full address cleaning + geocoding (paid endpoint, needs secret).

| Param | Type | Required | Description |
|---|---|---|---|
| `address` | string | yes |  |
| `secret` | string | yes |  |
| `token` | string | yes |  |

### `dadata_find_party_by_inn`

_Policy op:_ `drive.read`

DaData lookup company/IP by exact INN.

| Param | Type | Required | Description |
|---|---|---|---|
| `inn` | string | yes |  |
| `token` | string | yes |  |

### `dadata_suggest_address`

_Policy op:_ `drive.read`

DaData address autocomplete (КЛАДР/ФИАС-backed).

| Param | Type | Required | Description |
|---|---|---|---|
| `count` | integer | no |  |
| `query` | string | yes |  |
| `token` | string | yes |  |

### `dadata_suggest_bank`

_Policy op:_ `drive.read`

DaData bank autocomplete by name or BIC.

| Param | Type | Required | Description |
|---|---|---|---|
| `count` | integer | no |  |
| `query` | string | yes |  |
| `token` | string | yes |  |

### `dadata_suggest_party`

_Policy op:_ `drive.read`

DaData company / IP autocomplete by name or INN.

| Param | Type | Required | Description |
|---|---|---|---|
| `count` | integer | no |  |
| `query` | string | yes |  |
| `token` | string | yes |  |

---

## diadoc

_Diadoc_ — 4 tools.

### `diadoc_authenticate`

_Policy op:_ `drive.read`

Контур.Диадок password auth → auth_token.

| Param | Type | Required | Description |
|---|---|---|---|
| `api_key` | string | yes |  |
| `login` | string | yes |  |
| `password` | string | yes |  |

### `diadoc_docs_list`

_Policy op:_ `drive.read`

Диадок docs list. filter_category: Any.Inbound / Any.Outbound / UniversalTransferDocument.Inbound.NotFinished etc. Dates dd.MM.yyyy.

| Param | Type | Required | Description |
|---|---|---|---|
| `api_key` | string | yes |  |
| `auth_token` | string | yes |  |
| `box_id` | string | yes |  |
| `filter_category` | string | no |  |
| `from_date` | string | no |  |
| `to_date` | string | no |  |

### `diadoc_get_event`

_Policy op:_ `drive.read`

Диадок one event (document delivery / signature).

| Param | Type | Required | Description |
|---|---|---|---|
| `api_key` | string | yes |  |
| `auth_token` | string | yes |  |
| `box_id` | string | yes |  |
| `message_id` | string | yes |  |

### `diadoc_my_organizations`

_Policy op:_ `drive.read`

Диадок: orgs the user has access to.

| Param | Type | Required | Description |
|---|---|---|---|
| `api_key` | string | yes |  |
| `auth_token` | string | yes |  |

---

## docs

_Google Docs_ — 6 tools.

### `docs_append_text`

_Policy op:_ `docs.write`

Append a paragraph to the end of a Doc. Optional `style` for paragraph style: h1..h6, title, subtitle, normal. Default normal.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `document_id` | string | yes |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `style` | string | no |  |
| `text` | string | yes |  |

### `docs_create`

_Policy op:_ `docs.write`

Create a new empty Google Doc. Returns {document_id, title, url}. Optional parent_folder_id moves it into a Drive folder.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `parent_folder_id` | string | no |  |
| `title` | string | yes |  |

### `docs_export_pdf`

_Policy op:_ `docs.read`

Export the doc as PDF to a local path. Uses Drive's files.export under the hood.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dest_path` | string | yes |  |
| `document_id` | string | yes |  |

### `docs_insert_table`

_Policy op:_ `docs.write`

Insert a (rows × cols) table. If position_index is omitted, appends at the end of the document.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `cols` | integer | yes |  |
| `document_id` | string | yes |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `position_index` | integer | no |  |
| `rows` | integer | yes |  |

### `docs_read`

_Policy op:_ `docs.read`

Read a Doc's title, full plain text, and heading structure. body_text is capped at 50 000 chars; _meta.body_truncated flags overflows.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `document_id` | string | yes |  |

### `docs_replace_text`

_Policy op:_ `docs.write`

Find-and-replace text across the whole document. `replacements` is a dict like {'{client}': 'Иван Иванов', '{date}': '2026-05-20'}. Returns {replaced_count, per_needle}.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `document_id` | string | yes |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `match_case` | boolean | no |  |
| `replacements` | object | yes |  |

---

## drive

_Google Drive_ — 25 tools.

### `drive_add_comment`

_Policy op:_ `drive.write`

Add a comment to a Drive file (works on Docs/Sheets/Slides/PDFs). For anchored comments pass the JSON `anchor` string Drive expects; otherwise the comment is file-level.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `anchor` | string | no |  |
| `content` | string | yes |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `file_id` | string | yes |  |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |

### `drive_copy`

_Policy op:_ `drive.create`

Copy a Drive file.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `file_id` | string | yes |  |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `new_name` | string | no |  |
| `parent_id` | string | no |  |

### `drive_create_folder`

_Policy op:_ `drive.create`

Create a new folder inside parent_id.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `name` | string | yes |  |
| `parent_id` | string | yes |  |

### `drive_delete`

_Policy op:_ `drive.delete`

Permanently delete a Drive file (no trash).

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `file_id` | string | yes |  |

### `drive_download`

_Policy op:_ `drive.read`

Download a Drive file to a local path.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dest_path` | string | yes |  |
| `file_id` | string | yes |  |

### `drive_download_revision`

_Policy op:_ `drive.read`

Download a binary revision to `dest_path`. Works on PDFs, images, .xlsx uploads. Does NOT work on native Google formats (Sheets/Docs/Slides) — for those use revision metadata + Drive UI's version history.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dest_path` | string | yes |  |
| `file_id` | string | yes |  |
| `revision_id` | string | yes |  |

### `drive_empty_trash`

_Policy op:_ `drive.delete`

PERMANENTLY delete EVERYTHING in the trash. Irreversible — confirm with the user first.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |

### `drive_get_metadata`

_Policy op:_ `drive.read`

Get metadata for a Drive file by id.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `file_id` | string | yes |  |

### `drive_list_comments`

_Policy op:_ `drive.read`

List comments on a file. By default skips resolved comments; pass `include_resolved=true` to include them.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `file_id` | string | yes |  |
| `include_resolved` | boolean | no |  |

### `drive_list_files`

_Policy op:_ `drive.read`

List files in a Drive folder, newest first. folder_id='root' for My Drive root. `account` accepts alias / '*' / list of aliases for multi-account fan-out. Returns {files, _meta:{truncated, ...}}.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string \| array<string> | no | OAuth account: single alias, '*' (all), or list of aliases. |
| `folder_id` | string | no |  |
| `page_size` | integer | no | Max results, default 50, max 200 |
| `query` | string | no | Optional Drive query, e.g. "name contains 'report'" |

### `drive_list_permissions`

_Policy op:_ `drive.read`

List who has access to a Drive file. Returns {permissions: [{id, type, role, emailAddress, displayName}], _meta}. Use before share/revoke to know current state.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `file_id` | string | yes |  |

### `drive_list_revisions`

_Policy op:_ `drive.read`

List version history of a Drive file. Each revision has id, modifiedTime, lastModifyingUser, size, mimeType. For native Google formats Google auto-saves revisions; for binary uploads each update is a revision.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `file_id` | string | yes |  |

### `drive_list_shared`

_Policy op:_ `drive.read`

List files OTHER users shared with this account ('Shared with me'). Use when the file isn't in user's own My Drive. `account` accepts alias / '*' / list. Returns id, name, mimeType, modifiedTime, owners.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string \| array<string> | no | OAuth account: single alias, '*' (all), or list of aliases. |
| `page_size` | integer | no | Max results, default 50, max 200 |

### `drive_list_trash`

_Policy op:_ `drive.read`

List files currently in the trash. Returns {files, _meta:{truncated}}.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `page_size` | integer | no |  |

### `drive_move`

_Policy op:_ `drive.update`

Move a Drive file to a new parent folder.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `file_id` | string | yes |  |
| `new_parent_id` | string | yes |  |

### `drive_name_patterns`

_Policy op:_ `drive.read`

Structural analysis of file names matching a query — no contents read. Returns recurring codes, years, doc-types, frequent words. Call FIRST for 'из чего состоит X'. `account` accepts alias / '*' / list of aliases.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string \| array<string> | no | OAuth account: single alias, '*' (all), or list of aliases. |
| `query` | string | yes |  |

### `drive_rename`

_Policy op:_ `drive.update`

Rename a Drive file/folder.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `file_id` | string | yes |  |
| `new_name` | string | yes |  |

### `drive_resolve_comment`

_Policy op:_ `drive.write`

Mark a comment as resolved (Drive's 'Done' button).

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `comment_id` | string | yes |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `file_id` | string | yes |  |

### `drive_restore_from_trash`

_Policy op:_ `drive.write`

Restore a trashed file (sets trashed=false).

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `file_id` | string | yes |  |

### `drive_revoke_permission`

_Policy op:_ `drive.write`

Revoke a permission by its id (get it from drive_list_permissions).

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `file_id` | string | yes |  |
| `permission_id` | string | yes |  |

### `drive_search`

_Policy op:_ `drive.read`

Search Drive by name (owned + shared). mime_type shortcuts: spreadsheet|doc|folder|presentation|pdf|script|form. `account` accepts alias / '*' / list of aliases. Returns {files, _meta:{truncated, empty_reason}}. page_size default 50, max 200.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string \| array<string> | no | OAuth account: single alias, '*' (all), or list of aliases. |
| `mime_type` | string | no | Optional filter. Shortcuts: spreadsheet, doc, folder, presentation, pdf, script, form. Or full mime string. |
| `name_contains` | string | yes |  |

### `drive_share`

_Policy op:_ `drive.write`

Grant `email` access to `file_id` at `role` level. role = reader (view), commenter (view+comment), writer (edit), owner (transfers ownership, see drive_transfer_ownership). `notify=True` sends Google's default email; pass `message` to customize.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `email` | string | yes |  |
| `file_id` | string | yes |  |
| `message` | string | no |  |
| `notify` | boolean | no |  |
| `role` | string | no | reader\|commenter\|writer\|owner |

### `drive_transfer_ownership`

_Policy op:_ `drive.write`

Transfer ownership to `new_owner_email`. For consumer Gmail accounts the receiver gets a pending-ownership notification they must accept (pending_owner=True in the response signals this).

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `file_id` | string | yes |  |
| `new_owner_email` | string | yes |  |

### `drive_update_content`

_Policy op:_ `drive.update`

Replace the content of an existing Drive file from a local file.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `file_id` | string | yes |  |
| `local_path` | string | yes |  |
| `mime_type` | string | no |  |

### `drive_upload`

_Policy op:_ `drive.create`

Upload a local file to Drive folder parent_id.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `local_path` | string | yes |  |
| `mime_type` | string | no |  |
| `name` | string | no |  |
| `parent_id` | string | yes |  |

---

## duckdb

_Duckdb_ — 5 tools.

### `duckdb_drop_table`

_Policy op:_ `local.delete`

Drop a DuckDB table.

| Param | Type | Required | Description |
|---|---|---|---|
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `table` | string | yes |  |

### `duckdb_export_parquet`

_Policy op:_ `local.write`

Export a DuckDB table to a Parquet file.

| Param | Type | Required | Description |
|---|---|---|---|
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `path` | string | yes |  |
| `table` | string | yes |  |

### `duckdb_import_csv`

_Policy op:_ `local.write`

Import a local CSV into a DuckDB table. replace=True overwrites.

| Param | Type | Required | Description |
|---|---|---|---|
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `path` | string | yes |  |
| `replace` | boolean | no |  |
| `table` | string | yes |  |

### `duckdb_list_tables`

_Policy op:_ `local.read`

List all tables + row counts + columns in the local DuckDB.

_No parameters._

### `duckdb_query`

_Policy op:_ `local.read`

Run a SQL query against the local DuckDB. Supports `read_csv_auto('path')` inline. max_rows caps output.

| Param | Type | Required | Description |
|---|---|---|---|
| `max_rows` | integer | no |  |
| `sql` | string | yes |  |

---

## embed

_Embed_ — 1 tools.

### `embed_texts`

_Policy op:_ `local.read`

Embed texts with multilingual sentence-transformer. Default model: multilingual-e5-small.

| Param | Type | Required | Description |
|---|---|---|---|
| `model` | string | no |  |
| `texts` | array<string> | yes |  |

---

## excel

_Excel (.xlsx local)_ — 1 tools.

### `excel_parse`

_Policy op:_ `local.read`

Parse a local .xlsx file into row dicts. If `sheet` given, returns rows for that sheet only.

| Param | Type | Required | Description |
|---|---|---|---|
| `path` | string | yes |  |
| `sheet` | string | no |  |

---

## files

_File analyze / extract_ — 5 tools.

### `analyses_list`

_Policy op:_ `files.read`

List all saved file analyses (`.data/analyses/*.md`), newest first. Returns {analyses:[{name, path, source, focus, created_at, chars_in, file_kind}]}. Use to see what previous analyses are available before searching.

_No parameters._

### `analyses_read`

_Policy op:_ `files.read`

Read a saved analysis `.md` back into context. `name` is what analyses_list returns (with or without .md extension). Returns full content including YAML front-matter, synthesis, pass_a, pass_b.

| Param | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes | Analysis name from analyses_list (e.g. 'zoom_olga_2026-05-21'). |

### `analyses_search`

_Policy op:_ `files.read`

Semantic search across saved file analyses (subset of notes filtered by tag analysis:). Use when user asks about previous file analyses ('что мы вытащили из созвона Ольги?', 'какие боли мы находили у Иванова?'). Returns top_k hits with preview snippets; follow up with analyses_read(name) to get full .md.

| Param | Type | Required | Description |
|---|---|---|---|
| `query` | string | yes | Natural-language query in the user's language. |
| `top_k` | integer | no | Max results. Default 5. |

### `file_analyze_ensemble`

_Policy op:_ `files.read`

**3-LLM ensemble file analysis** (Phase 15). Use when user attached a file (PDF/DOCX/XLSX/Image/Audio) OR pasted Google Doc/Sheet URL AND asked for analysis. **TRIGGER PHRASES (RU/EN):** проанализируй / разбери / выдай сводку / резюмируй / что главное / боли клиента / рекомендации / приоритетные действия / факторный анализ / финансовый разбор / analyze / summarize / what's the main point / pain points / recommendations. Pipeline: Haiku (facts, parallel) + Sonnet (interpretation, parallel) → Sonnet-judge sees both + 5KB excerpt → synthesis. Output: structured Russian markdown (Главное / Боли / Рекомендации / Цифры / Расхождения between passes), ₽ tables, action items. ~30-90s wall-clock. **Uses claude CLI subscription auth — NO API key needed.** Saves `.md` to .data/analyses/ + indexes via notes.add for `analyses_search` later. For non-analysis intent (просто покажи / найди в файле X) → `file_extract_text` instead.

| Param | Type | Required | Description |
|---|---|---|---|
| `focus` | string | yes | What to extract/analyze (e.g. 'боли клиента + рекомендации финансиста', 'ключевые цифры и риски', 'action items'). Be specific — drives all 3 LLMs. |
| `max_chars` | integer | no | Cap input text before LLM. Default 100,000 chars. Lower if you want to save tokens. |
| `path_or_url` | string | yes | Local file path OR https URL (Google Docs/Sheets). |
| `save_as` | string | no | Optional name for the .md file. If omitted: auto-generated from source filename + UTC timestamp. |

### `file_extract_text`

_Policy op:_ `files.read`

Universal text extraction from any supported file or Google URL. Auto-routes by extension/URL: TXT/MD/CSV/PDF/XLSX/DOCX (local), PNG/JPG (OCR via Tesseract), MP3/M4A/WAV (Whisper API, requires OPENAI_API_KEY), Google Doc URL (Docs API), Google Sheet URL (structural summary). Returns {text, file_kind, source, chars, truncated, _meta}. Use this BEFORE file_analyze_ensemble if you want raw text without LLM cost, or to peek at file size.

| Param | Type | Required | Description |
|---|---|---|---|
| `kind` | string | no | Optional override of auto-detected kind ('text'/'pdf'/'docx'/'xlsx'/'image'/'audio'/'gdoc'/'gsheet'). |
| `max_chars` | integer | no | Cap output at N chars. Sets _meta.truncated=true if hit. |
| `path_or_url` | string | yes | Local file path OR https URL to Google Docs/Sheets/Drive file. |

---

## forms

_Google Forms_ — 4 tools.

### `forms_add_question`

_Policy op:_ `forms.write`

Append a question. question_type: text | paragraph | multiple_choice | checkbox | dropdown | scale | date. Choice types need `options`. Scale needs scale_low/high (and optional labels).

| Param | Type | Required | Description |
|---|---|---|---|
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `form_id` | string | yes |  |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `options` | array<string> | no |  |
| `paragraph` | boolean | no |  |
| `question_type` | string | yes |  |
| `required` | boolean | no |  |
| `scale_high` | integer | no |  |
| `scale_high_label` | string | no |  |
| `scale_low` | integer | no |  |
| `scale_low_label` | string | no |  |
| `title` | string | yes |  |

### `forms_create`

_Policy op:_ `forms.write`

Create a new Google Form. Returns {form_id, title, url, edit_url}.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `description` | string | no |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `parent_folder_id` | string | no |  |
| `title` | string | yes |  |

### `forms_read`

_Policy op:_ `forms.read`

Read a form's title, description, and question list.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `form_id` | string | yes |  |

### `forms_read_responses`

_Policy op:_ `forms.read`

Read submissions to a form. `since` filters by RFC3339 timestamp.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `form_id` | string | yes |  |
| `since` | string | no |  |

---

## fx

_Currency / FX_ — 1 tools.

### `fx_rate`

_Policy op:_ `web.read`

Fetch official RUB exchange rate for a currency from CBR.ru. `currency_code` is 3-letter ISO (USD, EUR, CNY...). date_iso optional (today by default). Returns {currency, date, rate_to_rub, nominal}.

| Param | Type | Required | Description |
|---|---|---|---|
| `currency_code` | string | yes |  |
| `date_iso` | string | no | YYYY-MM-DD; defaults to today. |

---

## gcp

_GCP project management_ — 4 tools.

### `gcp_enable_api`

_Policy op:_ `apps_script.edit`

Enable a Google Cloud API in our GCP project via Service Usage API — no Cloud Console click needed. `api_name` is the hostname, e.g. 'driveactivity.googleapis.com', 'logging.googleapis.com', 'script.googleapis.com', 'sheets.googleapis.com'. Idempotent.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `api_name` | string | yes |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `project_number` | string | no |  |

### `gcp_list_enabled_apis`

_Policy op:_ `apps_script.edit`

List all APIs enabled in our GCP project. Returns {count, apis: [...]}.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `project_number` | string | no |  |

### `gcp_list_projects`

_Policy op:_ `apps_script.edit`

List all GCP projects the calling account has access to. Returns [{project_id, project_number, name, state}].

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |

### `gcp_project_number`

_Policy op:_ `apps_script.edit`

Look up the numeric project_number for a project_id. Handy when you only remember the human-readable id but need the number for browser_set_script_gcp_project.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `project_id` | string | yes |  |

---

## gmail

_Gmail_ — 17 tools.

### `gmail_archive`

_Policy op:_ `gmail.write`

Archive a message (remove INBOX label).

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `message_id` | string | yes |  |

### `gmail_batch_modify`

_Policy op:_ `gmail.write`

Bulk label modify across many messages in ONE call. Use for «архивировать все письма от X старше года»: gmail_search → extract ids → batch_modify(remove=['INBOX']).

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `add` | array<string> | no |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `message_ids` | array<string> | yes |  |
| `remove` | array<string> | no |  |

### `gmail_create_draft`

_Policy op:_ `gmail.draft`

Create a DRAFT email (does NOT send). Always create a draft FIRST so the user can review; then call gmail_send_draft to actually send.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `bcc` | string | no |  |
| `body` | string | yes |  |
| `cc` | string | no |  |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `subject` | string | yes |  |
| `to` | string | yes |  |

### `gmail_create_filter`

_Policy op:_ `gmail.write`

Create a Gmail filter rule. `criteria` examples: {'from': 'noreply@github.com'}, {'subject': 'invoice', 'hasAttachment': true}, {'query': 'from:bank.com newer_than:30d'}. Actions: add_labels, remove_labels, forward_to.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `add_labels` | array<string> | no |  |
| `criteria` | object | yes |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `forward_to` | string | no |  |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `remove_labels` | array<string> | no |  |

### `gmail_delete_filter`

_Policy op:_ `gmail.write`

Delete a Gmail filter rule by id.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `filter_id` | string | yes |  |

### `gmail_download_attachment`

_Policy op:_ `gmail.read`

Save an attachment to a local path. Pass message_id and attachment_id from gmail_get_message.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `attachment_id` | string | yes |  |
| `dest_path` | string | yes |  |
| `message_id` | string | yes |  |

### `gmail_forward`

_Policy op:_ `gmail.draft`

Create a DRAFT forward of a message with original headers + body quoted below. Optional `body` is inserted before the quote.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `body` | string | no |  |
| `cc` | string | no |  |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `message_id` | string | yes |  |
| `to` | string | yes |  |

### `gmail_get_message`

_Policy op:_ `gmail.read`

Read a full message: headers, plain-text body (capped at 20k chars), and list of attachments. Returns body_text and the attachment list with attachment_ids you can download separately.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `message_id` | string | yes |  |

### `gmail_get_thread`

_Policy op:_ `gmail.read`

Read every message in a thread (oldest → newest). Returns {thread_id, messages: [{id, from, to, subject, date, snippet, body_text, ...}], _meta}. Critical context before replying to a multi-message conversation.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `thread_id` | string | yes |  |

### `gmail_list_filters`

_Policy op:_ `gmail.read`

List all Gmail filter rules. Each filter has id, criteria, action.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |

### `gmail_list_labels`

_Policy op:_ `gmail.read`

List Gmail labels (system + user-created). Useful for narrowing searches with 'label:foo' and for finding label IDs to pass to gmail_modify_labels.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |

### `gmail_mark_read`

_Policy op:_ `gmail.write`

Mark a message as read (remove UNREAD label).

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `message_id` | string | yes |  |

### `gmail_mark_unread`

_Policy op:_ `gmail.write`

Mark a message as unread (add UNREAD label).

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `message_id` | string | yes |  |

### `gmail_modify_labels`

_Policy op:_ `gmail.write`

Add or remove labels on a message. `add`/`remove` are lists of label IDs (use gmail_list_labels to resolve names to ids). System ids: INBOX, UNREAD, STARRED, IMPORTANT, SPAM, TRASH.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `add` | array<string> | no |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `message_id` | string | yes |  |
| `remove` | array<string> | no |  |

### `gmail_reply`

_Policy op:_ `gmail.draft`

Create a DRAFT reply to a message with correct threading headers (In-Reply-To, References). reply_all=true includes original To+Cc. Never sends — caller uses gmail_send_draft after user approval.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `body` | string | yes |  |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `message_id` | string | yes |  |
| `reply_all` | boolean | no |  |

### `gmail_search`

_Policy op:_ `gmail.read`

Search emails via Gmail query syntax: 'from:elena', 'has:attachment', 'subject:invoice', 'newer_than:7d'. Returns {messages, _meta:{total_count, truncated}}. **Default max_results=20** (cap 100); `_meta.total_count` is Gmail's estimate. If truncated, narrow query or raise max_results.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `max_results` | integer | no | Default 20, max 100. |
| `query` | string | yes |  |

### `gmail_send_draft`

_Policy op:_ `gmail.send`

Send a draft created by gmail_create_draft. SEPARATE call so the user gets one explicit approval prompt before any email actually leaves the account.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `draft_id` | string | yes |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |

---

## imap

_Imap_ — 2 tools.

### `imap_fetch_body`

_Policy op:_ `drive.read`

Fetch one IMAP message body + attachment list (size/filename, no payload).

| Param | Type | Required | Description |
|---|---|---|---|
| `folder` | string | no |  |
| `host` | string | yes |  |
| `password` | string | yes |  |
| `port` | integer | yes |  |
| `uid` | string | yes |  |
| `use_ssl` | boolean | no |  |
| `user` | string | yes |  |

### `imap_recent`

_Policy op:_ `drive.read`

List recent IMAP messages in a folder. Returns headers only — use imap_fetch_body for one message.

| Param | Type | Required | Description |
|---|---|---|---|
| `folder` | string | no |  |
| `host` | string | yes |  |
| `limit` | integer | no |  |
| `password` | string | yes |  |
| `port` | integer | yes |  |
| `since_days` | integer | no |  |
| `use_ssl` | boolean | no |  |
| `user` | string | yes |  |

---

## local

_Local filesystem_ — 6 tools.

### `local_extract_pdf_text`

_Policy op:_ `local.read`

Extract text from a PDF (pdfplumber). Returns {file_name, pages_count, text, chars, truncated}. `pages='1-3'`/`'5'` limits range. For BANK STATEMENTS prefer bank_parse_statement.

| Param | Type | Required | Description |
|---|---|---|---|
| `max_chars` | integer | no | Cap output (the tool wrapper truncates at 12k anyway). |
| `pages` | string | no | Page range, e.g. '1-3' or '5' or '1,3,5'. Omit for all pages. |
| `path` | string | yes |  |

### `local_image_info`

_Policy op:_ `local.read`

Get image metadata + a base64 data-URL preview suitable for sending to a multimodal model. Image is downscaled to max 1568px side. Returns {file_name, format, width, height, bytes, data_url}. Use to inspect screenshots, photos, or other images the user attaches.

| Param | Type | Required | Description |
|---|---|---|---|
| `path` | string | yes |  |

### `local_list_dir`

_Policy op:_ `local.read`

List entries in a local directory (shallow).

| Param | Type | Required | Description |
|---|---|---|---|
| `path` | string | yes |  |

### `local_read_file`

_Policy op:_ `local.read`

Read UTF-8 text file. Returns {content, total_lines, offset, returned_lines, has_more}. Chunked via offset+limit (0-indexed line offsets). Loop with offset=next_offset until has_more=False for >12k-char files.

| Param | Type | Required | Description |
|---|---|---|---|
| `limit` | integer | no | Max lines to return. Omit for whole file (still subject to ~12k-char tool cap — use chunks for big files). |
| `offset` | integer | no | 0-based line offset, default 0. |
| `path` | string | yes |  |

### `local_walk_dir`

_Policy op:_ `local.read`

Recursively list ALL files in a directory. Returns [{rel_path, size, suffix}]. Cuts off at max_files (default 500) so large repos don't blow up. Use when the user attaches a folder and you need to see what's inside.

| Param | Type | Required | Description |
|---|---|---|---|
| `include_hidden` | boolean | no |  |
| `max_files` | integer | no |  |
| `path` | string | yes |  |

### `local_write_file`

_Policy op:_ `local.write`

Write a local text file (creates parent dirs).

| Param | Type | Required | Description |
|---|---|---|---|
| `content` | string | yes |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `path` | string | yes |  |

---

## lock

_Lock_ — 3 tools.

### `lock_acquire`

_Policy op:_ `local.write`

Acquire a named lock (thread + file marker). Use to serialize destructive ops across parallel agent turns.

| Param | Type | Required | Description |
|---|---|---|---|
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `name` | string | yes |  |
| `owner` | string | no |  |
| `ttl_seconds` | integer | no |  |
| `wait_seconds` | integer | no |  |

### `lock_release`

_Policy op:_ `local.write`

Release a lock by its token (mismatch rejected).

| Param | Type | Required | Description |
|---|---|---|---|
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `name` | string | yes |  |
| `token` | string | yes |  |

### `lock_status`

_Policy op:_ `local.read`

Inspect a lock without acquiring.

| Param | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes |  |

---

## mdm

_Mdm_ — 4 tools.

### `mdm_delete`

_Policy op:_ `local.delete`

Remove an MDM record by id.

| Param | Type | Required | Description |
|---|---|---|---|
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `record_id` | string | yes |  |
| `table` | string | yes |  |

### `mdm_record_upsert`

_Policy op:_ `local.write`

Insert or merge an MDM record by id. external_ids carries marketplace cross-refs (wb_nm, ozon_sku).

| Param | Type | Required | Description |
|---|---|---|---|
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `external_ids` | object | no |  |
| `fields` | object | yes |  |
| `record_id` | string | yes |  |
| `table` | string | yes |  |

### `mdm_resolve`

_Policy op:_ `local.read`

Find an MDM record by external id (e.g. wb_nm). Returns the first match.

| Param | Type | Required | Description |
|---|---|---|---|
| `external_key` | string | yes |  |
| `external_value` | string | yes |  |
| `table` | string | yes |  |

### `mdm_table_get`

_Policy op:_ `local.read`

Read entire MDM table (products / suppliers / contractors).

| Param | Type | Required | Description |
|---|---|---|---|
| `table` | string | yes |  |

---

## moysklad

_Moysklad_ — 14 tools.

### `moysklad_cashflow_report`

_Policy op:_ `drive.read`

МС cashflow — money in/out grouped by day.

| Param | Type | Required | Description |
|---|---|---|---|
| `moment_from` | string | yes |  |
| `moment_to` | string | yes |  |
| `token` | string | yes |  |

### `moysklad_counterparties_list`

_Policy op:_ `drive.read`

МС контрагенты (customers + suppliers).

| Param | Type | Required | Description |
|---|---|---|---|
| `filter_str` | string | no |  |
| `limit` | integer | no |  |
| `offset` | integer | no |  |
| `token` | string | yes |  |

### `moysklad_customerorders_list`

_Policy op:_ `drive.read`

МС заказы покупателей. moment_from/to format `2026-05-01 00:00:00`.

| Param | Type | Required | Description |
|---|---|---|---|
| `limit` | integer | no |  |
| `moment_from` | string | no |  |
| `moment_to` | string | no |  |
| `offset` | integer | no |  |
| `token` | string | yes |  |

### `moysklad_demands_list`

_Policy op:_ `drive.read`

МС отгрузки (revenue recognition events).

| Param | Type | Required | Description |
|---|---|---|---|
| `limit` | integer | no |  |
| `moment_from` | string | no |  |
| `offset` | integer | no |  |
| `token` | string | yes |  |

### `moysklad_expenses_list`

_Policy op:_ `drive.read`

МС расходы (cashout).

| Param | Type | Required | Description |
|---|---|---|---|
| `limit` | integer | no |  |
| `moment_from` | string | no |  |
| `offset` | integer | no |  |
| `token` | string | yes |  |

### `moysklad_organizations_list`

_Policy op:_ `drive.read`

МС юр.лица.

| Param | Type | Required | Description |
|---|---|---|---|
| `token` | string | yes |  |

### `moysklad_products_list`

_Policy op:_ `drive.read`

МойСклад товары. `filter_str` is the МС filter DSL, e.g. `name~Шланг;archived=false`.

| Param | Type | Required | Description |
|---|---|---|---|
| `filter_str` | string | no |  |
| `limit` | integer | no |  |
| `offset` | integer | no |  |
| `token` | string | yes |  |

### `moysklad_profit_byproduct`

_Policy op:_ `drive.read`

МС прибыль по товарам — margin per SKU. Closest-to-truth unit-econ report МС provides.

| Param | Type | Required | Description |
|---|---|---|---|
| `limit` | integer | no |  |
| `moment_from` | string | yes |  |
| `moment_to` | string | yes |  |
| `token` | string | yes |  |

### `moysklad_services_list`

_Policy op:_ `drive.read`

МС услуги.

| Param | Type | Required | Description |
|---|---|---|---|
| `limit` | integer | no |  |
| `offset` | integer | no |  |
| `token` | string | yes |  |

### `moysklad_stock_all`

_Policy op:_ `drive.read`

МС остатки по всем складам.

| Param | Type | Required | Description |
|---|---|---|---|
| `limit` | integer | no |  |
| `offset` | integer | no |  |
| `token` | string | yes |  |

### `moysklad_stock_bystore`

_Policy op:_ `drive.read`

МС остатки в одном складе.

| Param | Type | Required | Description |
|---|---|---|---|
| `limit` | integer | no |  |
| `offset` | integer | no |  |
| `store_id` | string | yes |  |
| `token` | string | yes |  |

### `moysklad_stores_list`

_Policy op:_ `drive.read`

МС склады.

| Param | Type | Required | Description |
|---|---|---|---|
| `token` | string | yes |  |

### `moysklad_supplies_list`

_Policy op:_ `drive.read`

МС приёмки от поставщиков.

| Param | Type | Required | Description |
|---|---|---|---|
| `limit` | integer | no |  |
| `moment_from` | string | no |  |
| `offset` | integer | no |  |
| `token` | string | yes |  |

### `moysklad_variants_list`

_Policy op:_ `drive.read`

МС модификации (size/color SKU variants).

| Param | Type | Required | Description |
|---|---|---|---|
| `limit` | integer | no |  |
| `offset` | integer | no |  |
| `token` | string | yes |  |

---

## nlp

_Nlp_ — 5 tools.

### `nlp_extract_bik`

_Policy op:_ `local.read`

Extract Russian bank BIC codes (start with 04, 9 digits).

| Param | Type | Required | Description |
|---|---|---|---|
| `text` | string | yes |  |

### `nlp_extract_inns`

_Policy op:_ `local.read`

Extract Russian INN (10 or 12 digit) from text. validate=True keeps only valid FNS checksums.

| Param | Type | Required | Description |
|---|---|---|---|
| `text` | string | yes |  |
| `validate` | boolean | no |  |

### `nlp_extract_ogrn`

_Policy op:_ `local.read`

Extract Russian OGRN/OGRNIP (13 or 15 digits).

| Param | Type | Required | Description |
|---|---|---|---|
| `text` | string | yes |  |

### `nlp_extract_phones`

_Policy op:_ `local.read`

Extract Russian phone numbers; normalize=True → E.164-like `79991234567`.

| Param | Type | Required | Description |
|---|---|---|---|
| `normalize` | boolean | no |  |
| `text` | string | yes |  |

### `nlp_named_entities`

_Policy op:_ `local.read`

Full Natasha NER pass (org / person / location). Lazy-imports natasha; returns hint if not installed.

| Param | Type | Required | Description |
|---|---|---|---|
| `text` | string | yes |  |

---

## notes

_Agent notes (persistent memory)_ — 5 tools.

### `notes_add`

_Policy op:_ `notes.write`

Save a short note for future reference. Use for facts the user shares that you'll want later: IDs, preferences, recurring constants ('Лена 2026 НДС 5%', 'ID финального отчёта = 1AbC…'). Optional tag groups related notes. Always proactively save such facts.

| Param | Type | Required | Description |
|---|---|---|---|
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `tag` | string | no | Optional grouping tag like 'elena', 'taxes', 'ids'. |
| `text` | string | yes |  |

### `notes_list`

_Policy op:_ `notes.read`

List all stored notes, oldest first. Use to refresh your memory at the start of a session if the user references things you should know.

| Param | Type | Required | Description |
|---|---|---|---|
| `limit` | integer | no | Default 50. |

### `notes_remove`

_Policy op:_ `notes.write`

Delete a note by id. Use when the user explicitly asks to forget something.

| Param | Type | Required | Description |
|---|---|---|---|
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `id` | integer | yes |  |

### `notes_search`

_Policy op:_ `notes.read`

Find notes by substring across text and tag. Check this when the user asks about something they previously told you ('что я говорил про НДС?', 'какой был ID той презентации?').

| Param | Type | Required | Description |
|---|---|---|---|
| `query` | string | yes |  |

### `notes_search_semantic`

_Policy op:_ `notes.read`

Semantic search across notes (local embeddings). Better than notes_search for fuzzy queries ('налоги' → notes about НДС). Falls back to substring if embedding model unavailable; `_meta.search_method` flags which.

| Param | Type | Required | Description |
|---|---|---|---|
| `query` | string | yes |  |
| `top_k` | integer | no |  |

---

## notify

_Notify_ — 2 tools.

### `notify_mark_delivered`

_Policy op:_ `local.write`

Record that a notification was actually sent on a channel.

| Param | Type | Required | Description |
|---|---|---|---|
| `channel` | string | yes |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `notification_id` | string | yes |  |
| `result` | string | no |  |

### `notify_route`

_Policy op:_ `local.write`

Stage a notification. level: info | warning | error | critical. channels: ['telegram_ops','email_finance',...].

| Param | Type | Required | Description |
|---|---|---|---|
| `channels` | array<string> | no |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `level` | string | yes |  |
| `message` | string | yes |  |

---

## ocr

_Ocr_ — 2 tools.

### `ocr_image`

_Policy op:_ `local.read`

OCR an image. engine: tesseract (default, local) or paddle (more accurate on Cyrillic).

| Param | Type | Required | Description |
|---|---|---|---|
| `engine` | string | no |  |
| `image_path` | string | yes |  |
| `lang` | string | no |  |

### `ocr_pdf`

_Policy op:_ `local.read`

OCR every page of a scanned PDF. For digitally-born PDFs prefer file_extract.

| Param | Type | Required | Description |
|---|---|---|---|
| `lang` | string | no |  |
| `pdf_path` | string | yes |  |

---

## onec

_Onec_ — 5 tools.

### `onec_contractors`

_Policy op:_ `drive.read`

1С Catalog_Контрагенты filtered by name substring.

| Param | Type | Required | Description |
|---|---|---|---|
| `base_url` | string | yes |  |
| `login` | string | yes |  |
| `name_like` | string | no |  |
| `password` | string | yes |  |
| `top` | integer | no |  |

### `onec_documents`

_Policy op:_ `drive.read`

1С documents. doc_type example: Document_РеализацияТоваровУслуг. date_from OData datetime `2026-05-01T00:00:00`.

| Param | Type | Required | Description |
|---|---|---|---|
| `base_url` | string | yes |  |
| `date_from` | string | no |  |
| `doc_type` | string | yes |  |
| `login` | string | yes |  |
| `password` | string | yes |  |
| `top` | integer | no |  |

### `onec_money_balance`

_Policy op:_ `drive.read`

1С AccumulationRegister_ДенежныеСредстваБалансе — cash balance snapshot.

| Param | Type | Required | Description |
|---|---|---|---|
| `base_url` | string | yes |  |
| `date_iso` | string | no |  |
| `login` | string | yes |  |
| `password` | string | yes |  |

### `onec_odata_query`

_Policy op:_ `drive.read`

Generic 1С OData GET. `path`: entity name (e.g. Catalog_Контрагенты). filter_: OData filter syntax.

| Param | Type | Required | Description |
|---|---|---|---|
| `base_url` | string | yes |  |
| `filter_` | string | no |  |
| `login` | string | yes |  |
| `password` | string | yes |  |
| `path` | string | yes |  |
| `select` | string | no |  |
| `skip` | integer | no |  |
| `top` | integer | no |  |

### `onec_products`

_Policy op:_ `drive.read`

1С Catalog_Номенклатура.

| Param | Type | Required | Description |
|---|---|---|---|
| `base_url` | string | yes |  |
| `login` | string | yes |  |
| `password` | string | yes |  |
| `skip` | integer | no |  |
| `top` | integer | no |  |

---

## open

_Open external app_ — 1 tools.

### `open_url`

_Policy op:_ `local.write`

Open `url` in the user's default browser. Use for 'открой эту таблицу' / 'покажи мне в браузере'.

| Param | Type | Required | Description |
|---|---|---|---|
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `url` | string | yes |  |

---

## ozon

_Ozon_ — 12 tools.

### `ozon_analytics_data`

_Policy op:_ `drive.read`

Daily analytics via /v1/analytics/data. metrics: revenue, ordered_units, delivered_units, returns, cancellations, hits_view_search, hits_view_pdp. dimension: day, week, month, sku, brand, category1-4.

| Param | Type | Required | Description |
|---|---|---|---|
| `api_key` | string | yes |  |
| `client_id` | string | yes |  |
| `date_from` | string | yes |  |
| `date_to` | string | yes |  |
| `dimension` | array<string> | no |  |
| `metrics` | array<string> | no |  |

### `ozon_check_credentials`

_Policy op:_ `drive.read`

Cheapest call to verify Ozon (Client-Id, Api-Key) pair. Returns {ok, credentials_valid, _meta:{http_status, ratelimit}}. Call BEFORE batch fetches.

| Param | Type | Required | Description |
|---|---|---|---|
| `api_key` | string | yes |  |
| `client_id` | string | yes |  |

### `ozon_finance_realization`

_Policy op:_ `drive.read`

Monthly realization report (отчёт о реализации) via /v2/finance/realization.

| Param | Type | Required | Description |
|---|---|---|---|
| `api_key` | string | yes |  |
| `client_id` | string | yes |  |
| `month` | integer | yes |  |
| `year` | integer | yes |  |

### `ozon_finance_transactions`

_Policy op:_ `drive.read`

Detailed transactions via /v3/finance/transaction/list. Dates RFC3339. Pass `operation_type` array to filter (e.g. ['OperationAgentDeliveredToCustomer']).

| Param | Type | Required | Description |
|---|---|---|---|
| `api_key` | string | yes |  |
| `client_id` | string | yes |  |
| `date_from` | string | yes |  |
| `date_to` | string | yes |  |
| `operation_type` | array<string> | no |  |
| `page` | integer | no |  |
| `page_size` | integer | no |  |

### `ozon_orders_fbo_list`

_Policy op:_ `drive.read`

FBO postings via /v2/posting/fbo/list. Dates RFC3339 (`2026-05-01T00:00:00Z`). Includes analytics_data + financial_data.

| Param | Type | Required | Description |
|---|---|---|---|
| `api_key` | string | yes |  |
| `client_id` | string | yes |  |
| `date_from` | string | yes |  |
| `date_to` | string | yes |  |
| `limit` | integer | no |  |
| `offset` | integer | no |  |

### `ozon_orders_fbs_list`

_Policy op:_ `drive.read`

FBS postings via /v3/posting/fbs/list. Optional `status`: awaiting_packaging / awaiting_deliver / delivered / cancelled.

| Param | Type | Required | Description |
|---|---|---|---|
| `api_key` | string | yes |  |
| `client_id` | string | yes |  |
| `date_from` | string | yes |  |
| `date_to` | string | yes |  |
| `limit` | integer | no |  |
| `offset` | integer | no |  |
| `status` | string | no |  |

### `ozon_prices_list`

_Policy op:_ `drive.read`

Current prices via /v4/product/info/prices. Paginated.

| Param | Type | Required | Description |
|---|---|---|---|
| `api_key` | string | yes |  |
| `client_id` | string | yes |  |
| `last_id` | string | no |  |
| `limit` | integer | no |  |

### `ozon_products_list`

_Policy op:_ `drive.read`

Product list via /v3/product/list. visibility: ALL, VISIBLE, INVISIBLE, EMPTY_STOCK, NOT_MODERATED, ARCHIVED. Paginated by `last_id`.

| Param | Type | Required | Description |
|---|---|---|---|
| `api_key` | string | yes |  |
| `client_id` | string | yes |  |
| `last_id` | string | no |  |
| `limit` | integer | no |  |
| `visibility` | string | no |  |

### `ozon_returns_list`

_Policy op:_ `drive.read`

Returns (возвраты) via /v1/returns/company/fbo. Dates RFC3339.

| Param | Type | Required | Description |
|---|---|---|---|
| `api_key` | string | yes |  |
| `client_id` | string | yes |  |
| `date_from` | string | yes |  |
| `date_to` | string | yes |  |
| `limit` | integer | no |  |
| `offset` | integer | no |  |

### `ozon_stocks_fbo`

_Policy op:_ `drive.read`

FBO stocks via /v4/product/info/stocks. Paginated by cursor — pass `last_id` from previous response.

| Param | Type | Required | Description |
|---|---|---|---|
| `api_key` | string | yes |  |
| `client_id` | string | yes |  |
| `cursor` | string | no |  |
| `limit` | integer | no |  |

### `ozon_stocks_fbs`

_Policy op:_ `drive.read`

FBS stocks for specific SKUs via /v1/product/info/stocks-by-warehouse/fbs.

| Param | Type | Required | Description |
|---|---|---|---|
| `api_key` | string | yes |  |
| `client_id` | string | yes |  |
| `sku` | array<string> | no |  |

### `ozon_warehouses_list`

_Policy op:_ `drive.read`

FBS warehouses via /v1/warehouse/list.

| Param | Type | Required | Description |
|---|---|---|---|
| `api_key` | string | yes |  |
| `client_id` | string | yes |  |

---

## pandera

_Pandera_ — 1 tools.

### `pandera_validate`

_Policy op:_ `local.read`

Validate a list of dict-records against a Pandera DataFrameSchema (JSON-encoded). Returns row-level errors.

| Param | Type | Required | Description |
|---|---|---|---|
| `records` | array<object> | yes |  |
| `schema_json` | string | yes |  |

---

## pdf

_PDF generation_ — 1 tools.

### `pdf_create`

_Policy op:_ `local.write`

Generate a PDF locally via reportlab. kind='text' (string content), 'table' ({headers, rows}), or 'report' ({title, sections: [{heading, paragraphs, table?}]}). Supports Cyrillic via system fonts.

| Param | Type | Required | Description |
|---|---|---|---|
| `content` | any | yes |  |
| `dest_path` | string | yes |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `kind` | string | no |  |
| `title` | string | no |  |

---

## pochta

_Pochta_ — 5 tools.

### `pochta_normalize_address`

_Policy op:_ `drive.read`

Address normalizer — parses a free-form address string into components + delivery-area metadata.

| Param | Type | Required | Description |
|---|---|---|---|
| `address` | string | yes |  |
| `token` | string | yes |  |

### `pochta_order_get`

_Policy op:_ `drive.read`

Single Pochta order detail by id.

| Param | Type | Required | Description |
|---|---|---|---|
| `order_id` | integer | yes |  |
| `token` | string | yes |  |

### `pochta_orders_search`

_Policy op:_ `drive.read`

Search otpravka-api orders by recipient name / order-num.

| Param | Type | Required | Description |
|---|---|---|---|
| `limit` | integer | no |  |
| `query` | string | yes |  |
| `token` | string | yes |  |

### `pochta_tariff_calc`

_Policy op:_ `drive.read`

Tariff calculator. mass_g grams, indexes are 6-digit postal codes. mail_type: POSTAL_PARCEL / ONLINE_PARCEL / EMS.

| Param | Type | Required | Description |
|---|---|---|---|
| `index_from` | string | yes |  |
| `index_to` | string | yes |  |
| `mail_category` | string | no |  |
| `mail_type` | string | no |  |
| `mass_g` | integer | yes |  |
| `token` | string | yes |  |

### `pochta_track`

_Policy op:_ `drive.read`

Track a single Russian Post barcode. login+password are your tracking.pochta.ru account; we base64-encode them per API spec.

| Param | Type | Required | Description |
|---|---|---|---|
| `barcode` | string | yes |  |
| `login` | string | yes |  |
| `password` | string | yes |  |
| `token` | string | yes |  |

---

## reply

_Reply lint_ — 1 tools.

### `reply_self_check`

_Policy op:_ `verify.read`

Lint a draft reply BEFORE emitting: detects unattributed numbers (≥4 digits without nearby Sheet!A1 / file_id / provenance hint), false-completeness claims when a recent tool was truncated, and currency tokens without cell address. Returns {ok, warnings: [{kind, span, snippet, suggestion}], _meta}. Pass `recent_meta_flags` (list of `_meta` dicts from this turn's tool calls) for the truncation check.

| Param | Type | Required | Description |
|---|---|---|---|
| `draft_reply` | string | yes |  |
| `recent_meta_flags` | array<object> | no | Optional. List of _meta dicts from this turn's tool results. |

---

## report

_Reports_ — 7 tools.

### `report_combine`

_Policy op:_ `notes.write`

Merge saved reports into one row set keyed by `merge_key`; `sum_cols` summed across rows with same key. Optional `save_as` persists. Use for: monthly bank → yearly; per-store sales → company-wide. Returns {merged_count, sources, rows}.

| Param | Type | Required | Description |
|---|---|---|---|
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `keep_first_cols` | array<string> | no | Columns to keep value from first occurrence (default: all non-numeric). |
| `kind` | string | no | Optional: restrict source lookups to this kind. |
| `merge_key` | string | yes | Column name to group by (e.g. 'sku', 'counterparty', 'date'). |
| `names` | array<string> | yes | Saved-report names to merge. |
| `save_as` | string | no | Optional name to save the merged result as a new report (kind='combined'). |
| `sum_cols` | array<string> | no | Numeric columns to sum across reports. |

### `report_delete`

_Policy op:_ `notes.write`

Delete a saved report by name (optionally within a kind).

| Param | Type | Required | Description |
|---|---|---|---|
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `kind` | string | no |  |
| `name` | string | yes |  |

### `report_list`

_Policy op:_ `notes.read`

List all saved reports. Filter by kind if given. Returns [{name, kind, saved_at, bytes, metadata_keys}], newest first.

| Param | Type | Required | Description |
|---|---|---|---|
| `kind` | string | no |  |
| `limit` | integer | no |  |

### `report_load`

_Policy op:_ `notes.read`

Load a saved report by name. Returns the full payload {name, kind, saved_at, metadata, data}. Pass `kind` to disambiguate if the same name exists in multiple kinds.

| Param | Type | Required | Description |
|---|---|---|---|
| `kind` | string | no |  |
| `name` | string | yes |  |

### `report_render_csv`

_Policy op:_ `local.write`

Render a CSV report. headers + rows. Returns {path, bytes, row_count}.

| Param | Type | Required | Description |
|---|---|---|---|
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `headers` | array<string> | yes |  |
| `out_path` | string | yes |  |
| `rows` | array<array> | yes |  |

### `report_render_markdown`

_Policy op:_ `local.write`

Render a markdown report. sections = [{heading, body}]. Body is plain markdown.

| Param | Type | Required | Description |
|---|---|---|---|
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `out_path` | string | yes |  |
| `sections` | array<object> | yes |  |
| `title` | string | yes |  |

### `report_save`

_Policy op:_ `notes.write`

Save structured data (rows/stats/parsed) to `.data/reports/<kind>/<name>.json`. `kind` namespaces (e.g. 'bank', 'abc', 'sales'). For typed data the agent loads back later. E.g. after bank_parse_statement: save_report(name='varychev_alfa_dec_2025', kind='bank', data=transactions).

| Param | Type | Required | Description |
|---|---|---|---|
| `data` | any | yes | JSON-serializable data — list of dicts or a dict. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `kind` | string | yes | Namespace: 'bank', 'abc', 'sales', 'expenses', etc. |
| `metadata` | object | no | Optional context: source file, date range, account, etc. |
| `name` | string | yes | Unique within the kind. Use kebab/snake case. |

---

## sbis

_Sbis_ — 4 tools.

### `sbis_auth`

_Policy op:_ `drive.read`

СБИС.Аутентифицировать — login+password → session_id.

| Param | Type | Required | Description |
|---|---|---|---|
| `login` | string | yes |  |
| `password` | string | yes |  |

### `sbis_changes_since`

_Policy op:_ `drive.read`

СБИС.СписокИзменений for delta sync. since_iso ISO8601 with timezone.

| Param | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | yes |  |
| `since_iso` | string | yes |  |

### `sbis_doc_get`

_Policy op:_ `drive.read`

СБИС single document detail.

| Param | Type | Required | Description |
|---|---|---|---|
| `doc_id` | string | yes |  |
| `session_id` | string | yes |  |

### `sbis_docs_list`

_Policy op:_ `drive.read`

СБИС.СписокДокументов. doc_type: ВходящийДокумент / ИсходящийДокумент. Dates DD.MM.YYYY.

| Param | Type | Required | Description |
|---|---|---|---|
| `doc_type` | string | no |  |
| `from_date` | string | no |  |
| `limit` | integer | no |  |
| `session_id` | string | yes |  |
| `to_date` | string | no |  |

---

## scheduler

_Scheduler_ — 4 tools.

### `scheduler_cancel`

_Policy op:_ `local.write`

Cancel a pending scheduled task.

| Param | Type | Required | Description |
|---|---|---|---|
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `task_id` | string | yes |  |

### `scheduler_complete`

_Policy op:_ `local.write`

Mark a scheduled task done.

| Param | Type | Required | Description |
|---|---|---|---|
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `result_note` | string | no |  |
| `task_id` | string | yes |  |

### `scheduler_due`

_Policy op:_ `local.read`

List pending tasks with run_at ≤ until_iso (default now).

| Param | Type | Required | Description |
|---|---|---|---|
| `until_iso` | string | no |  |

### `scheduler_enqueue`

_Policy op:_ `local.write`

Record a scheduled task. This is a hint — the harness must poll scheduler_due.

| Param | Type | Required | Description |
|---|---|---|---|
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `payload` | object | no |  |
| `run_at_iso` | string | yes |  |
| `task` | string | yes |  |

---

## self

_Self-heal / introspection_ — 9 tools.

### `self_edit_source`

_Policy op:_ `self.edit`

Replace the contents of a source file (under `src/` or `static/`). USER APPROVAL REQUIRED. After editing, ALWAYS call self_smoke_test to verify the change still imports cleanly. The running process keeps old code in memory until the user restarts the app.

| Param | Type | Required | Description |
|---|---|---|---|
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `new_content` | string | yes | Full file content. Make sure to preserve existing code outside the area you're fixing. |
| `path` | string | yes |  |

### `self_git_commit`

_Policy op:_ `self.commit`

Stage given `paths` (or all changed tracked files if omitted) and commit with `message`. USER APPROVAL REQUIRED. Adds a 'Co-Authored-By: Claude (self-healing)' line automatically.

| Param | Type | Required | Description |
|---|---|---|---|
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `message` | string | yes |  |
| `paths` | array<string> | no |  |

### `self_git_diff`

_Policy op:_ `self.diff`

Show pending changes vs HEAD. `staged=True` shows the index; default shows the working tree. `path` narrows to one file. Returns {diff, files_changed, truncated}.

| Param | Type | Required | Description |
|---|---|---|---|
| `path` | string | no |  |
| `staged` | boolean | no |  |

### `self_git_revert`

_Policy op:_ `self.revert`

Discard unstaged changes to `path` (git checkout HEAD -- path). USER APPROVAL REQUIRED. Use when self_smoke_test failed after an edit.

| Param | Type | Required | Description |
|---|---|---|---|
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `path` | string | yes |  |

### `self_git_status`

_Policy op:_ `self.diff`

`git status --short` — list modified / untracked files. Cheap, no approval.

_No parameters._

### `self_list_tools`

_Policy op:_ `self.read`

Introspect every registered tool. Returns {tools: [{name, policy_op, description, has_account_param}], _meta}. Useful for the agent to self-orient on its own capabilities.

_No parameters._

### `self_read_source`

_Policy op:_ `self.read`

Read a source file from this project (under `src/` or `static/`). Returns {path, content, lines, bytes}. Use as the first step when fixing a bug in the agent itself or its UI.

| Param | Type | Required | Description |
|---|---|---|---|
| `path` | string | yes |  |

### `self_run_tests`

_Policy op:_ `self.test`

Run pytest on a pattern (default 'tests/test_*.py'). Beyond self_smoke_test (imports only), this exercises actual test cases. Use after self_edit before self_git_commit.

| Param | Type | Required | Description |
|---|---|---|---|
| `deselect` | array<string> | no |  |
| `pattern` | string | no |  |

### `self_smoke_test`

_Policy op:_ `self.test`

Spawn a fresh Python and verify `src.app` imports cleanly. Returns {ok, exit_code, stdout, stderr}. ALWAYS run after self_edit_source — catches syntax errors and missing imports.

_No parameters._

---

## sheets

_Google Sheets_ — 46 tools.

### `sheets_add_protected_range`

_Policy op:_ `sheets.write`

Protect a range from edits. `warning_only=true` → confirm prompt but lets edits through; default false blocks all but listed `editors` (emails). Omit editors → only owner. Returns {protected_range_id} for later removal.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `description` | string | no |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `editors` | array<string> | no |  |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `range` | string | yes |  |
| `spreadsheet_id` | string | yes |  |
| `warning_only` | boolean | no |  |

### `sheets_add_sheet`

_Policy op:_ `sheets.write`

Add a new tab/sheet to an existing spreadsheet.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `spreadsheet_id` | string | yes |  |
| `title` | string | yes |  |

### `sheets_append_rows`

_Policy op:_ `sheets.write`

Append rows below existing data in the given range.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `range` | string | yes |  |
| `spreadsheet_id` | string | yes |  |
| `values` | array<array> | yes |  |

### `sheets_batch_read`

_Policy op:_ `sheets.read`

Read MULTIPLE ranges in ONE HTTP (values.batchGet). E.g. ['Sheet1!B45', 'Sheet2!C12:C18']. Returns {per_range: [{range, values, row_count, empty}], _meta}. Prefer over many read_range when consolidating across tabs.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `formatted` | boolean | no |  |
| `ranges` | array<string> | yes | A1 ranges. Empty list returns empty per_range. |
| `spreadsheet_id` | string | yes |  |

### `sheets_bulk_metric`

_Policy op:_ `sheets.read`

Parallel cell-read across N spreadsheets sharing the same layout (Phase 14A). For N≥5 books, this is the right call — burns 1 API token/book at ThreadPoolExecutor(10) parallelism. Discover `cell` FIRST via `sheets_metric_lookup(representative_id, metric)` → take its `.cell` output → pass here. **No full-scan fallback** — `cell` is REQUIRED. For N>50, prefer `sheets_cross_aggregate` (1 Apps Script round-trip). Pass `dry_run=true` to see cost estimate before executing. Returns compacted {stats, outliers (top 10/bottom 10), errors (first 5), _meta.result_token}. Drill down to full per-book data via `bulk_load_results(result_token)`.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `cell` | string | yes | Full A1 ref, typically from metric_lookup output. E.g. 'Год факт!B45' or 'B45'. |
| `dry_run` | boolean | no | If true, return cost estimate without executing. |
| `formatted` | boolean | no | Default false (numbers as numbers). True → string as displayed in UI. |
| `max_workers` | integer | no | Parallel workers, clamped to [1, 16]. Default 10. |
| `spreadsheet_ids` | array<string> | yes | List of spreadsheet IDs. Caller asserts they share layout. |

### `sheets_bulk_read`

_Policy op:_ `sheets.read`

Parallel read of arbitrary {spreadsheet_id, range} pairs across N books (Phase 14B). For batch reads where each cell/range can differ — e.g. «pull A1:E5 from book X AND Год факт!B45 from book Y». ThreadPoolExecutor(10) parallelism. Per-ref errors isolated. **For same-cell-across-many-books, prefer `sheets_bulk_metric`.** Returns compacted {stats over scalar values, outliers, per-ref dims}; full per-ref `values` grids spilled — retrieve via `bulk_load_results`. Pass `dry_run=true` for cost preview.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no |  |
| `formatted` | boolean | no | Default value mode for refs that don't override. False = raw numbers. |
| `max_workers` | integer | no | Parallel workers, clamped to [1, 16]. Default 10. |
| `refs` | array<object> | yes | List of {spreadsheet_id, range, formatted?} dicts. ≥1 item. |

### `sheets_clear_range`

_Policy op:_ `sheets.write`

Clear all values in a range.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `range` | string | yes |  |
| `spreadsheet_id` | string | yes |  |

### `sheets_copy_sheet_to`

_Policy op:_ `sheets.write`

Copy a tab from one spreadsheet to ANOTHER spreadsheet. `source_sheet` accepts the tab title (string) OR the numeric sheetId. Destination gets a new sheet titled 'Copy of …' — rename via batchUpdate if needed.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dest_spreadsheet_id` | string | yes |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `source_sheet` | ['string', 'integer'] | yes | Tab title OR numeric sheetId. |
| `source_spreadsheet_id` | string | yes |  |

### `sheets_create_chart`

_Policy op:_ `sheets.write`

Insert a chart. chart_type: line|bar|column|pie|area|scatter. domain_range = A1 for X axis (one column). series_ranges = list of A1 ranges, one per Y series. position_row/col control where the chart is anchored. Returns {chart_id}.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `chart_type` | string | yes |  |
| `domain_range` | string | yes |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `position_col` | integer | no |  |
| `position_row` | integer | no |  |
| `position_sheet` | ['string', 'integer'] | no |  |
| `series_ranges` | array<string> | yes |  |
| `sheet` | ['string', 'integer'] | yes |  |
| `spreadsheet_id` | string | yes |  |
| `title` | string | yes |  |

### `sheets_create_named_range`

_Policy op:_ `sheets.write`

Define a new named range in a spreadsheet. range example: 'Sheet1!B45' (single cell) or 'Sheet1!B45:B45'. Useful when tidying up a workbook so future agents (and users) can refer to key metrics by name.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `name` | string | yes |  |
| `range` | string | yes |  |
| `spreadsheet_id` | string | yes |  |

### `sheets_create_pivot`

_Policy op:_ `sheets.write`

Create a pivot table. source_range must include a header row. rows/columns are lists of header NAMES (not letters). values is list of {column: <header>, aggregate: SUM|AVERAGE|COUNT|MAX|MIN, name?: <label>}. If dest_sheet is omitted, a new tab is created.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `columns` | array<string> | no |  |
| `dest_cell` | string | no |  |
| `dest_sheet` | ['string', 'integer'] | no |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `rows` | array<string> | yes |  |
| `source_range` | string | yes |  |
| `spreadsheet_id` | string | yes |  |
| `values` | array<object> | no |  |

### `sheets_create_spreadsheet`

_Policy op:_ `sheets.write`

Create a brand-new spreadsheet.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `title` | string | yes |  |

### `sheets_cross_aggregate`

_Policy op:_ `sheets.read`

Server-side aggregation across N books via persistent Apps Script (Phase 14C). For N≥50 OR aggregates (sum/avg/min/max across many books). Chunks N into batches of `chunk_size` (default 100), runs `max_concurrent` chunks in parallel (default 5). At N=500 with defaults: 5 chunks × ~70s each running in parallel → ~70-100s total. Returns {value: <aggregate>, stats, _meta}. **Requires one-time setup** — see docs/PHASE_14_SETUP.md. First call fails with Phase14ConfigError if script_id not configured. Pass `dry_run=true` for cost preview.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `cell` | string | yes | A1 ref (e.g. 'B45'). Must match in every book. |
| `chunk_size` | integer | no | Books per Apps Script call. Default 100. Each chunk takes ~60-90s. Keep ≤150 to fit under Google L7 LB timeout. |
| `dry_run` | boolean | no |  |
| `max_concurrent` | integer | no | Parallel Apps Script invocations. Default 5. |
| `max_iterations` | integer | no | Per-chunk resumption cap. Default 5. |
| `op` | string | no | Aggregation operation. Default 'sum'. |
| `sheet` | string | yes | Tab name (e.g. 'Год факт'). Must match in every book. |
| `spreadsheet_ids` | array<string> | yes | List of spreadsheet IDs sharing layout. |

### `sheets_cross_aggregate_status`

_Policy op:_ `sheets.read`

Peek at progress of an incomplete sheets_cross_aggregate run by its resume token. Returns {status: 'incomplete'|'not_found', processed_count, remaining_count}. Use only when cross_aggregate exhausted max_iterations and you want to see how far it got.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `token` | string | yes | Resume token from a prior incomplete cross_aggregate response. |

### `sheets_diff_snapshot`

_Policy op:_ `sheets.read`

Compare two sheets_snapshot_range() results. Returns {rows_added, rows_removed, cells_changed, diff_examples, new_tail_rows}.

| Param | Type | Required | Description |
|---|---|---|---|
| `after` | object | yes |  |
| `before` | object | yes |  |
| `max_examples` | integer | no |  |

### `sheets_duplicate_sheet`

_Policy op:_ `sheets.write`

Duplicate a sheet/tab inside the SAME spreadsheet. `source_sheet` accepts the tab title (string) OR the numeric sheetId. Returns {new_sheet_id, title, index}. For copying between different spreadsheets use sheets_copy_sheet_to.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `new_name` | string | yes |  |
| `source_sheet` | ['string', 'integer'] | yes | Tab title OR numeric sheetId. |
| `spreadsheet_id` | string | yes |  |

### `sheets_excel_to_sheets`

_Policy op:_ `sheets.write`

End-to-end: parse .xlsx → create new Google Spreadsheet → optionally move to parent_folder_id → copy every sheet (names preserved). Returns spreadsheet_id + url. Replaces excel_parse + create_spreadsheet + N write_range calls.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `local_path` | string | yes |  |
| `parent_folder_id` | string | no | Optional Drive folder to move the new spreadsheet into. |
| `title` | string | no | Optional; defaults to the xlsx filename without extension. |

### `sheets_find_and_replace`

_Policy op:_ `sheets.write`

Sheets-native find-and-replace via batchUpdate — one call, no read/write cycle. Auto-snapshots affected scope first (recoverable via sheets_rollback). Optional `sheet` to limit to one tab. Supports match_case, match_entire_cell, use_regex flags.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `find` | string | yes |  |
| `match_case` | boolean | no |  |
| `match_entire_cell` | boolean | no |  |
| `replace` | string | yes |  |
| `sheet` | string | no | Optional tab name; if omitted, replaces in all sheets. |
| `spreadsheet_id` | string | yes |  |
| `use_regex` | boolean | no |  |

### `sheets_find_in_spreadsheet`

_Policy op:_ `sheets.read`

Substring search across EVERY sheet. Returns {matches: [{sheet, cell, row, col, value, row_label?, col_label?}], _meta}. `with_labels=true` attaches col A + row 1 labels. For metric+period lookup prefer sheets_metric_lookup.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `case_sensitive` | boolean | no | Default false. |
| `query` | string | yes |  |
| `spreadsheet_id` | string | yes |  |
| `with_labels` | boolean | no | When true, each match also carries row_label (col A) and col_label (row 1) — use to verify the metric/period the cell actually belongs to. |

### `sheets_freeze`

_Policy op:_ `sheets.write`

Freeze N rows + M cols of a sheet so they stay pinned while scrolling. `sheet` can be the tab title or numeric sheetId. Most common: rows=1 to pin the header row.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `cols` | integer | no |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `rows` | integer | no |  |
| `sheet` | ['string', 'integer'] | yes |  |
| `spreadsheet_id` | string | yes |  |

### `sheets_get_cell_notes`

_Policy op:_ `sheets.read`

Read notes attached to cells in a range. Returns {notes: 2D array of strings/None, _meta}. Empty cells return None at that position.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `range` | string | yes |  |
| `spreadsheet_id` | string | yes |  |

### `sheets_get_metadata`

_Policy op:_ `sheets.read`

Get spreadsheet metadata: title and list of sheets/tabs.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `spreadsheet_id` | string | yes |  |

### `sheets_iter_rows`

_Policy op:_ `sheets.read`

Paginated read: `chunk_size` rows from data row `offset` (0-based, excludes header). Returns {rows, offset, next_offset, has_more}. Only when per-row inspection is needed and QUERY/PROFILE/SCRIPT won't work. Loop until has_more=False. Default chunk=200, max 5000.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `chunk_size` | integer | no | Default 200, max 5000. |
| `columns` | string | no | Column range, default 'A:ZZ'. |
| `offset` | integer | no | 0-based data row offset (skip header automatically). |
| `sheet` | string | yes | Just the tab name (no '!' or range). |
| `spreadsheet_id` | string | yes |  |

### `sheets_last_data_row`

_Policy op:_ `sheets.read`

Find the last non-empty row in a column. Unlike summarize().grid.rows (which is sheet DIMENSION, often inflated), this is the actual data extent.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `column` | string | no |  |
| `sheet` | string | yes |  |
| `spreadsheet_id` | string | yes |  |

### `sheets_list_backups`

_Policy op:_ `sheets.read`

List recent automatic snapshots taken before write/clear/find_and_replace operations on a spreadsheet. Each entry has snapshot_id, ts, range, op.

| Param | Type | Required | Description |
|---|---|---|---|
| `limit` | integer | no | Default 20. |
| `spreadsheet_id` | string | yes |  |

### `sheets_list_named_ranges`

_Policy op:_ `sheets.read`

List named ranges in a spreadsheet. Call FIRST for metric lookup — if `Чистая_прибыль_Год` exists, read it directly. Returns {named_ranges: [{name, sheet, range, named_range_id}], _meta}.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `spreadsheet_id` | string | yes |  |

### `sheets_list_protected_ranges`

_Policy op:_ `sheets.read`

List every protected range in a spreadsheet. Returns {protected_ranges: [{protected_range_id, description, warning_only, sheet, range, editors}], _meta}.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `spreadsheet_id` | string | yes |  |

### `sheets_merge_cells`

_Policy op:_ `sheets.write`

Merge a rectangular range. merge_type: MERGE_ALL (default — one big cell), MERGE_COLUMNS (rows merge per-column), MERGE_ROWS (cols merge per-row).

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `merge_type` | string | no |  |
| `range` | string | yes |  |
| `spreadsheet_id` | string | yes |  |

### `sheets_metric_lookup`

_Policy op:_ `sheets.read`

ONE-CALL resolver: single metric+period cell. Tries named ranges → find_with_labels → period filter. Returns {value, cell, row_label, col_label}. **For aggregates (SUM/COUNT/GROUP BY/topN) use sheets_query, not this.** Ambiguity → returns candidates. For N≥5 books with the same layout: call this ONCE on a representative book to get `cell`, then `sheets_bulk_metric(rest, cell)` — never loop metric_lookup over many books.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `metric` | string | yes | Row-label keyword: 'Чистая прибыль', 'Выручка', 'Остаток'. |
| `period` | string | no | Optional column-header keyword: 'Год факт', 'Декабрь 2025', 'Q1'. Omit for the LAST populated column in the row (typical YTD). |
| `spreadsheet_id` | string | yes |  |

### `sheets_period_detect`

_Policy op:_ `sheets.read`

Classify each column in the header row. Returns {periods: [{col, col_letter, label, kind}], _meta}. kind ∈ {month, quarter, year, plan_fact, other}. Use to find «какая колонка декабрь 2025» without guessing.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `header_row` | integer | no | 1-based row index. Default 1. |
| `sheet` | string | yes |  |
| `spreadsheet_id` | string | yes |  |

### `sheets_profile`

_Policy op:_ `sheets.write`

Server-side column stats: name, non_blank, blank, distinct, type, top_5, min/max/avg for numeric. No raw row fetch. Use BEFORE reading large/unfamiliar sheets. policy=write (temp sheet, auto-cleaned).

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `sheet` | string | yes | Tab name. |
| `spreadsheet_id` | string | yes |  |

### `sheets_query`

_Policy op:_ `sheets.write`

Server-side aggregation: SELECT/WHERE/GROUP BY/ORDER BY/LIMIT against a range. **For SUM/COUNT/AVG/GROUP/topN over millions of rows.** For a single metric+period cell prefer sheets_metric_lookup. 10k-row cap: `_meta.truncated=true` → narrow WHERE. policy=write (hidden temp sheet). `response_format="concise"` (default) trims to first 50 rows of the result; pass "detailed" for the full grid.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `response_format` | string | no | concise: first 50 result rows (default, token-efficient). detailed: full grid up to 10k rows. |
| `source_range` | string | yes | Range like 'Orders!A:M' or 'Orders' (whole sheet). First row is treated as headers. |
| `spreadsheet_id` | string | yes |  |
| `sql` | string | yes | QUERY language, e.g. 'SELECT A, SUM(C) WHERE B > 100 GROUP BY A ORDER BY SUM(C) DESC LIMIT 20' |

### `sheets_read_named_range`

_Policy op:_ `sheets.read`

Read the values stored at a named range by name. Returns {values, _meta:{name, range_read}}. The most reliable way to look up a labelled metric — no fuzzy match, no risk of grabbing the wrong row.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `formatted` | boolean | no |  |
| `name` | string | yes | Named range name, e.g. 'Чистая_прибыль_Год'. |
| `spreadsheet_id` | string | yes |  |

### `sheets_read_range`

_Policy op:_ `sheets.read`

Read a range. range example: 'Sheet1!A1:C100'. Returns {values, _meta:{range_read, empty_reason, value_mode}}. `formatted=true` → values as displayed (e.g. '3 087 967 ₽'); default raw. For finding a known metric prefer `sheets_metric_lookup`.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `formatted` | boolean | no | Return values as displayed (currency symbols, date formats) instead of raw. |
| `range` | string | yes |  |
| `spreadsheet_id` | string | yes |  |

### `sheets_remove_protected_range`

_Policy op:_ `sheets.write`

Remove a protected range by its numeric protectedRangeId (get it from sheets_list_protected_ranges).

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `protected_range_id` | integer | yes |  |
| `spreadsheet_id` | string | yes |  |

### `sheets_rollback`

_Policy op:_ `sheets.write`

Restore a previously saved snapshot. If snapshot_id is omitted, uses the most recent snapshot. The affected range is cleared and rewritten with the snapshot's values. Use when the user says 'отмени' / 'верни как было'.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `snapshot_id` | string | no | Optional. Omit to use the most recent snapshot. |
| `spreadsheet_id` | string | yes |  |

### `sheets_run_formula`

_Policy op:_ `sheets.write`

Evaluate any Sheets formula (e.g. =GOOGLEFINANCE("CURRENCY:USDRUB"), =IMPORTRANGE(...), =YEAR(TODAY())) WITHOUT creating a permanent cell. Uses temp hidden sheet — auto-cleaned. policy_op=sheets.write because temp sheet briefly mutates the file.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `formula` | string | yes | Must start with '='. E.g. '=GOOGLEFINANCE("CURRENCY:USDRUB")'. |
| `spreadsheet_id` | string | yes |  |

### `sheets_set_cell_note`

_Policy op:_ `sheets.write`

Attach a hover-shown 'note' to a cell or range. Distinct from Drive comments (drive_add_comment is for file-level discussion threads). Pass `note=''` to clear.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `note` | string | yes |  |
| `range` | string | yes |  |
| `spreadsheet_id` | string | yes |  |

### `sheets_set_conditional_format`

_Policy op:_ `sheets.write`

Add a conditional-format rule. condition: `negatives_red`, `positives_green`, `less_than` (needs threshold), `greater_than` (needs threshold), `text_contains` (needs text). Optional custom `color` dict {red, green, blue: 0..1}.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `color` | object | no |  |
| `condition` | string | yes |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `range` | string | yes |  |
| `spreadsheet_id` | string | yes |  |
| `text` | string | no |  |
| `threshold` | number | no |  |

### `sheets_set_data_validation`

_Policy op:_ `sheets.write`

Attach a validation rule to a range. kind=`dropdown` (needs `values` list), `number_between` (needs min_value+max_value), `checkbox`, or `remove` (clears existing rules).

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `kind` | string | yes |  |
| `max_value` | number | no |  |
| `min_value` | number | no |  |
| `range` | string | yes |  |
| `show_dropdown` | boolean | no |  |
| `spreadsheet_id` | string | yes |  |
| `strict` | boolean | no |  |
| `values` | array<string> | no |  |

### `sheets_set_format`

_Policy op:_ `sheets.write`

Apply formatting to a range. `preset` ∈ {currency_rub, currency_rub_int, currency_usd, currency_eur, percent, percent_int, date_iso, date_ru, datetime_ru, number_2dp, number_int, text} OR raw `number_format` dict. Also `background_color`={r,g,b: 0..1}, `text_format`={bold, italic, fontSize}.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `background_color` | object | no |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `number_format` | object | no |  |
| `preset` | string | no | currency_rub \| currency_rub_int \| percent \| date_ru \| etc. |
| `range` | string | yes |  |
| `spreadsheet_id` | string | yes |  |
| `text_format` | object | no |  |

### `sheets_snapshot_range`

_Policy op:_ `sheets.read`

Take a structural snapshot of a sheet range (all values + dimensions). Cheap, one read. Pair with sheets_diff_snapshot(before, after) to verify what a script wrote.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `range` | string | yes |  |
| `spreadsheet_id` | string | yes |  |

### `sheets_summarize`

_Policy op:_ `sheets.read`

ONE-call structural summary: each sheet's name + grid + header + N sample rows (default 5, max 50). `_meta.data_rows_estimate` = real extent; `grid.rows` is sheet DIMENSION (padded). `_meta.is_sample=true` → slice only. Use FIRST; then narrow with sheets_metric_lookup.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `sample_rows` | integer | no | How many data rows to include per sheet (default 5, max 50). |
| `spreadsheet_id` | string | yes |  |

### `sheets_unmerge_cells`

_Policy op:_ `sheets.write`

Undo merges inside a range.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `range` | string | yes |  |
| `spreadsheet_id` | string | yes |  |

### `sheets_write_and_verify`

_Policy op:_ `sheets.write`

`write_range` + automatic verification: snapshot before + write + read-back + cell-by-cell diff. Returns {verdict: 'ok'|'modified', discrepancies}. Prefer this over `write_range` when the value must be confirmed (financial cells, agent's own writes that user will trust).

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `range` | string | yes |  |
| `spreadsheet_id` | string | yes |  |
| `values` | array<array> | yes |  |

### `sheets_write_range`

_Policy op:_ `sheets.write`

Overwrite a range with values (list of rows). Formulas allowed.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `range` | string | yes |  |
| `spreadsheet_id` | string | yes |  |
| `values` | array<array> | yes |  |

---

## skill

_Skill_ — 3 tools.

### `skill_list`

_Policy op:_ `local.read`

List registered skills (optional tag filter).

| Param | Type | Required | Description |
|---|---|---|---|
| `tag` | string | no |  |

### `skill_register`

_Policy op:_ `local.write`

Register a named skill — a bundle of tool names + prose. Builds an index of capabilities.

| Param | Type | Required | Description |
|---|---|---|---|
| `description` | string | yes |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `name` | string | yes |  |
| `tags` | array<string> | no |  |
| `tools` | array<string> | yes |  |

### `skill_remove`

_Policy op:_ `local.delete`

Remove a skill from the registry.

| Param | Type | Required | Description |
|---|---|---|---|
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `name` | string | yes |  |

---

## slides

_Google Slides_ — 7 tools.

### `slides_add_slide`

_Policy op:_ `slides.write`

Add a new slide. layout: BLANK | TITLE | TITLE_AND_BODY | TITLE_AND_TWO_COLUMNS | SECTION_HEADER | etc. position is 0-indexed; None appends.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `layout` | string | no |  |
| `position` | integer | no |  |
| `presentation_id` | string | yes |  |

### `slides_create`

_Policy op:_ `slides.write`

Create a new empty Google Slides presentation. Returns {presentation_id, title, url, slide_count}.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `parent_folder_id` | string | no |  |
| `title` | string | yes |  |

### `slides_create_from_template`

_Policy op:_ `slides.write`

Copy a template presentation, rename copy to dest_title, replace all {placeholder} strings across every slide. The most common Slides workflow.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dest_folder_id` | string | no |  |
| `dest_title` | string | yes |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `replacements` | object | yes |  |
| `template_id` | string | yes |  |

### `slides_export_pdf`

_Policy op:_ `slides.read`

Export the presentation as PDF to a local path. Uses Drive's files.export under the hood.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dest_path` | string | yes |  |
| `presentation_id` | string | yes |  |

### `slides_read`

_Policy op:_ `slides.read`

Read a presentation's title + per-slide text + structure. Returns {title, slides: [{slide_id, text, object_count}], _meta}.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `presentation_id` | string | yes |  |

### `slides_replace_image`

_Policy op:_ `slides.write`

Replace an image (by object ID) with a new image fetched from new_url. Find object IDs via slides_read and inspecting slides[].pageElements.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `image_object_id` | string | yes |  |
| `new_url` | string | yes |  |
| `presentation_id` | string | yes |  |

### `slides_replace_placeholders`

_Policy op:_ `slides.write`

Find-and-replace text across every slide. `replacements` example: {'{title}': 'Q1 2026', '{client}': 'Иван'}.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `match_case` | boolean | no |  |
| `presentation_id` | string | yes |  |
| `replacements` | object | yes |  |

---

## smsc

_Smsc_ — 3 tools.

### `smsc_balance`

_Policy op:_ `drive.read`

SMSC.ru balance.

| Param | Type | Required | Description |
|---|---|---|---|
| `login` | string | yes |  |
| `password` | string | yes |  |

### `smsc_send`

_Policy op:_ `gmail.send`

Send SMS via SMSC.ru. `phones` comma-separated. `sender` optional (must be pre-approved).

| Param | Type | Required | Description |
|---|---|---|---|
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `login` | string | yes |  |
| `mes` | string | yes |  |
| `password` | string | yes |  |
| `phones` | string | yes |  |
| `sender` | string | no |  |

### `smsc_status`

_Policy op:_ `drive.read`

SMSC.ru per-message status.

| Param | Type | Required | Description |
|---|---|---|---|
| `login` | string | yes |  |
| `password` | string | yes |  |
| `phone` | string | yes |  |
| `sms_id` | string | yes |  |

---

## smsru

_Smsru_ — 3 tools.

### `smsru_balance`

_Policy op:_ `drive.read`

SMS.ru balance (RUB).

| Param | Type | Required | Description |
|---|---|---|---|
| `api_id` | string | yes |  |

### `smsru_send`

_Policy op:_ `gmail.send`

Send SMS via SMS.ru. `test=1` simulates without spending balance.

| Param | Type | Required | Description |
|---|---|---|---|
| `api_id` | string | yes |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `from_` | string | no |  |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `msg` | string | yes |  |
| `test` | integer | no |  |
| `to` | string | yes |  |

### `smsru_status`

_Policy op:_ `drive.read`

Delivery status of one SMS by sms_id.

| Param | Type | Required | Description |
|---|---|---|---|
| `api_id` | string | yes |  |
| `sms_id` | string | yes |  |

---

## tasks

_Google Tasks_ — 7 tools.

### `tasks_complete`

_Policy op:_ `tasks.write`

Mark a task as completed.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `list_id` | string | yes |  |
| `task_id` | string | yes |  |

### `tasks_create`

_Policy op:_ `tasks.write`

Create a new task. `due` accepts 'YYYY-MM-DD' or RFC3339.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `due` | string | no |  |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `list_id` | string | yes |  |
| `notes` | string | no |  |
| `title` | string | yes |  |

### `tasks_create_list`

_Policy op:_ `tasks.write`

Create a new task list.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `title` | string | yes |  |

### `tasks_delete`

_Policy op:_ `tasks.write`

Permanently delete a task.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `list_id` | string | yes |  |
| `task_id` | string | yes |  |

### `tasks_list`

_Policy op:_ `tasks.read`

List tasks in a list. By default hides completed. Optional due_min/due_max filter by RFC3339 timestamp.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `due_max` | string | no |  |
| `due_min` | string | no |  |
| `list_id` | string | yes |  |
| `show_completed` | boolean | no |  |

### `tasks_list_lists`

_Policy op:_ `tasks.read`

List all task lists. Each list has id + title.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |

### `tasks_uncomplete`

_Policy op:_ `tasks.write`

Mark a completed task back as needsAction.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `list_id` | string | yes |  |
| `task_id` | string | yes |  |

---

## team

_Team_ — 1 tools.

### `team_channel_send`

_Policy op:_ `local.write`

Unified team-channel dispatcher. Stages a notification + returns routing decision pointing at the right send tool (tg_send_message / gmail_create_draft / smsru_send). Centralizes channel selection.

| Param | Type | Required | Description |
|---|---|---|---|
| `attachments` | array<object> | no |  |
| `channel` | string | yes | Prefixed: telegram_ops, email_finance, sms_alerts, ... |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `level` | string | no |  |
| `message` | string | yes |  |

---

## tg

_Tg_ — 4 tools.

### `tg_get_me`

_Policy op:_ `drive.read`

Verify Telegram bot token; returns bot identity.

| Param | Type | Required | Description |
|---|---|---|---|
| `bot_token` | string | yes |  |

### `tg_get_updates`

_Policy op:_ `drive.read`

Poll for incoming Telegram bot updates. `offset` = last update_id + 1.

| Param | Type | Required | Description |
|---|---|---|---|
| `bot_token` | string | yes |  |
| `offset` | integer | no |  |
| `timeout` | integer | no |  |

### `tg_send_message`

_Policy op:_ `gmail.send`

Post a message to a Telegram chat via Bot API. parse_mode: HTML or MarkdownV2.

| Param | Type | Required | Description |
|---|---|---|---|
| `bot_token` | string | yes |  |
| `chat_id` | integer \| string | yes |  |
| `disable_web_page_preview` | boolean | no |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `parse_mode` | string | no |  |
| `text` | string | yes |  |

### `tg_send_photo`

_Policy op:_ `gmail.send`

Send a photo to Telegram chat by URL.

| Param | Type | Required | Description |
|---|---|---|---|
| `bot_token` | string | yes |  |
| `caption` | string | no |  |
| `chat_id` | integer \| string | yes |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `photo_url` | string | yes |  |

---

## tinkoff

_Tinkoff_ — 4 tools.

### `tinkoff_check_order`

_Policy op:_ `drive.read`

Tinkoff /CheckOrder — every payment attempt for OrderId.

| Param | Type | Required | Description |
|---|---|---|---|
| `order_id` | string | yes |  |
| `password` | string | yes |  |
| `terminal_key` | string | yes |  |

### `tinkoff_get_customer`

_Policy op:_ `drive.read`

Tinkoff /GetCustomer — saved-card / customer profile.

| Param | Type | Required | Description |
|---|---|---|---|
| `customer_key` | string | yes |  |
| `password` | string | yes |  |
| `terminal_key` | string | yes |  |

### `tinkoff_get_state`

_Policy op:_ `drive.read`

Tinkoff /GetState — single-payment status. Returns Status + ErrorCode.

| Param | Type | Required | Description |
|---|---|---|---|
| `password` | string | yes |  |
| `payment_id` | string | yes |  |
| `terminal_key` | string | yes |  |

### `tinkoff_get_terminal_payouts`

_Policy op:_ `drive.read`

Tinkoff /GetTerminalPayouts — settlement money out for date range. Dates `2026-05-01`.

| Param | Type | Required | Description |
|---|---|---|---|
| `from_date` | string | yes |  |
| `password` | string | yes |  |
| `terminal_key` | string | yes |  |
| `to_date` | string | yes |  |

---

## trace

_Trace_ — 2 tools.

### `trace_recent`

_Policy op:_ `local.read`

Recent spans, substring + since filters.

| Param | Type | Required | Description |
|---|---|---|---|
| `limit` | integer | no |  |
| `name_like` | string | no |  |
| `since_iso` | string | no |  |

### `trace_span_log`

_Policy op:_ `local.write`

Append a span to the local trace log. OpenTelemetry-shaped {span_id, parent_span_id, name, duration_ms, attributes}.

| Param | Type | Required | Description |
|---|---|---|---|
| `attributes` | object | no |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `duration_ms` | number | yes |  |
| `parent_span_id` | string | no |  |
| `span_name` | string | yes |  |

---

## translate

_Translation_ — 2 tools.

### `translate`

_Policy op:_ `local.read`

Translate text offline via Argos Translate. First call to a new language pair downloads ~100MB. source_lang auto-detected (ru if Cyrillic, else en) if omitted.

| Param | Type | Required | Description |
|---|---|---|---|
| `source_lang` | string | no |  |
| `target_lang` | string | yes |  |
| `text` | string | yes |  |

### `translate_probe`

_Policy op:_ `local.read`

Check whether Argos Translate is installed. Returns {available, info}.

_No parameters._

---

## tspl

_Tspl_ — 1 tools.

### `tspl_render_label`

_Policy op:_ `local.write`

Same as zpl_render_label but for TSPL (Godex / TSC).

| Param | Type | Required | Description |
|---|---|---|---|
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `fields` | object | yes |  |
| `out_path` | string | yes |  |
| `template` | string | yes |  |

---

## verify

_Claim verification_ — 1 tools.

### `verify_claim`

_Policy op:_ `verify.read`

Defensive verifier (rules 19-23). Re-reads each source NOW, returns {verdict: ok|mismatch|error, discrepancies}. source_refs: compact strings like 'sheets:SID:Год факт!B45=3087967', 'named:SID:Profit=3087967', 'drive:FID=Title', 'gmail:MSG=invoice', 'calendar:EVT=weekly' — OR dict form. Mix freely.

| Param | Type | Required | Description |
|---|---|---|---|
| `claim` | string | yes | What the agent is about to assert (for logging). |
| `source_refs` | array<['object', 'string']> | yes | Each entry: either compact 'kind:scope:loc=expected' string OR {kind, ...fields} dict. |

---

## vision

_Vision (image analysis)_ — 2 tools.

### `vision_ocr`

_Policy op:_ `local.read`

OCR an image via Tesseract. `lang`='rus+eng' (default), 'rus', 'eng'. `structured=True` returns per-word bounding boxes. Requires Tesseract binary installed (see vision.py docstring).

| Param | Type | Required | Description |
|---|---|---|---|
| `image_path` | string | yes |  |
| `lang` | string | no |  |
| `structured` | boolean | no |  |

### `vision_probe`

_Policy op:_ `local.read`

Check whether Tesseract is reachable. Returns {available, info}.

_No parameters._

---

## vk

_Vk_ — 6 tools.

### `vk_ads_get_campaigns`

_Policy op:_ `drive.read`

VK ad campaigns list.

| Param | Type | Required | Description |
|---|---|---|---|
| `access_token` | string | yes |  |
| `account_id` | integer | yes |  |

### `vk_groups_get_members`

_Policy op:_ `drive.read`

VK group members list.

| Param | Type | Required | Description |
|---|---|---|---|
| `access_token` | string | yes |  |
| `count` | integer | no |  |
| `group_id` | string | yes |  |
| `offset` | integer | no |  |

### `vk_messages_send`

_Policy op:_ `gmail.send`

VK private message. peer_id: user / chat(2000000000+id) / group(-id).

| Param | Type | Required | Description |
|---|---|---|---|
| `access_token` | string | yes |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `message` | string | yes |  |
| `peer_id` | integer | yes |  |
| `random_id` | integer | no |  |

### `vk_users_get`

_Policy op:_ `drive.read`

Resolve VK user IDs / screen-names to profile data.

| Param | Type | Required | Description |
|---|---|---|---|
| `access_token` | string | yes |  |
| `fields` | string | no |  |
| `user_ids` | array<string> | yes |  |

### `vk_wall_get`

_Policy op:_ `drive.read`

VK wall posts. owner_id negative = group, positive = user.

| Param | Type | Required | Description |
|---|---|---|---|
| `access_token` | string | yes |  |
| `count` | integer | no |  |
| `offset` | integer | no |  |
| `owner_id` | integer | yes |  |

### `vk_wall_post`

_Policy op:_ `gmail.send`

VK wall post.

| Param | Type | Required | Description |
|---|---|---|---|
| `access_token` | string | yes |  |
| `attachments` | string | no |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `idempotency_key` | string | no | Optional client-supplied idempotency key (Stripe-style). On retry with the same key + same args within 24h, the cached response is replayed and the tool is N... |
| `message` | string | yes |  |
| `owner_id` | integer | yes |  |

---

## watcher

_Drive watcher_ — 4 tools.

### `watcher_list_alerts`

_Policy op:_ `notes.read`

List queued failure alerts (newest first). Pass unread_only=True to see only fresh ones. Each alert: {id, script_label, function, kind, timestamp, message, read}.

| Param | Type | Required | Description |
|---|---|---|---|
| `limit` | integer | no |  |
| `unread_only` | boolean | no |  |

### `watcher_mark_alerts_read`

_Policy op:_ `notes.write`

Mark alerts as read. If alert_ids is omitted, marks ALL as read.

| Param | Type | Required | Description |
|---|---|---|---|
| `alert_ids` | array<string> | no |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |

### `watcher_poll_known_scripts`

_Policy op:_ `apps_script.edit`

Scan ALL monitored scripts (Mylib + everything in the bound-script registry) for failures and append new ones to the alerts queue. Background watcher runs this every 5 min automatically; call manually to force an immediate check. Idempotent (won't duplicate).

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `since_minutes` | integer | no |  |

### `watcher_recent_failures`

_Policy op:_ `apps_script.edit`

Recent Apps Script failures for `script_id` via Cloud Logging (Exception, SyntaxError, 429, etc). Returns {failures: [{timestamp, function, execution_id, severity, kind, message}]}. Use for 'проверь не падал ли X' or after deploy.

| Param | Type | Required | Description |
|---|---|---|---|
| `account` | string | no | OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases. |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `script_id` | string | yes |  |
| `since_minutes` | integer | no |  |

---

## wb

_Wildberries (WB)_ — 15 tools.

### `wb_adverts_count`

_Policy op:_ `drive.read`

Count of advertising campaigns. `status`: -1=pause,4=ready,7=done,8=draft,9=active. `type_`: 4=catalog,5=cards,6=search,7=recommendation,8=auto,9=search-catalog.

| Param | Type | Required | Description |
|---|---|---|---|
| `status` | integer | no |  |
| `token` | string | yes |  |
| `type_` | integer | no |  |

### `wb_analytics_paid_storage`

_Policy op:_ `drive.read`

Paid-storage cost report (FBO) for [date_from..date_to]. Use for unit-economy: what each SKU costs in warehouse fees.

| Param | Type | Required | Description |
|---|---|---|---|
| `date_from` | string | yes |  |
| `date_to` | string | yes |  |
| `token` | string | yes |  |

### `wb_check_token`

_Policy op:_ `drive.read`

Ping every Wildberries API family (content/analytics/statistics/advert/marketplace/common) with `token`. Returns {family: {code, status}}. Use to verify a token has the expected access scopes BEFORE running a long fetch.

| Param | Type | Required | Description |
|---|---|---|---|
| `token` | string | yes |  |

### `wb_feedbacks_count`

_Policy op:_ `drive.read`

Count of customer reviews (отзывы).

| Param | Type | Required | Description |
|---|---|---|---|
| `is_answered` | boolean | no |  |
| `token` | string | yes |  |

### `wb_feedbacks_list`

_Policy op:_ `drive.read`

List customer reviews. `order`: dateDesc | dateAsc. Returns {data:{feedbacks:[{id, productValuation, text, ...}]}}.

| Param | Type | Required | Description |
|---|---|---|---|
| `is_answered` | boolean | no |  |
| `order` | string | no |  |
| `skip` | integer | no |  |
| `take` | integer | no |  |
| `token` | string | yes |  |

### `wb_finance_detail_collect`

_Policy op:_ `drive.read`

Fetch WB reportDetailByPeriod for [date_from..date_to]. Paginated by rrd_id, 65s pause between pages (WB rate-limit 1 req/min), honors X-Ratelimit-Retry. Returns {rows_count, last_rrd_id, pages, sample_first, sample_last}. Will raise immediately if recent run consumed budget — 12h ban risk.

| Param | Type | Required | Description |
|---|---|---|---|
| `date_from` | string | yes | YYYY-MM-DD |
| `date_to` | string | no | YYYY-MM-DD, defaults to today UTC |
| `limit` | integer | no |  |
| `max_pages` | integer | no | Stop after N pages — useful for testing. |
| `sleep_sec` | integer | no | Pause between pages. |
| `start_rrd_id` | integer | no |  |
| `token` | string | yes |  |

### `wb_orders_recent`

_Policy op:_ `drive.read`

Recent FBO+FBS orders since `date_from`. `flag=0` (default): delta since last call; `flag=1`: full window. Returns {ok, data: [{date, lastChangeDate, supplierArticle, nmId, ...}], _meta:{ratelimit}}.

| Param | Type | Required | Description |
|---|---|---|---|
| `date_from` | string | yes | YYYY-MM-DD |
| `flag` | integer | no | 0=delta since last call, 1=full window. |
| `token` | string | yes |  |

### `wb_prices_list`

_Policy op:_ `drive.read`

Current seller prices + discounts via `/api/v2/list/goods/filter`. Paginated. Returns {listGoods: [{nmID, vendorCode, sizes:[{discountedPrice, price}], ...}]}.

| Param | Type | Required | Description |
|---|---|---|---|
| `limit` | integer | no | Max 1000. |
| `offset` | integer | no |  |
| `token` | string | yes |  |

### `wb_questions_count`

_Policy op:_ `drive.read`

Count of buyer questions (FBO/FBS). Use `is_answered=false` for backlog. Quick SLA-monitoring tool.

| Param | Type | Required | Description |
|---|---|---|---|
| `is_answered` | boolean | no |  |
| `token` | string | yes |  |

### `wb_questions_list`

_Policy op:_ `drive.read`

List buyer questions. `date_from`/`date_to` are UNIX timestamps (seconds). Returns {data:{questions:[{id, text, nmId, productDetails, ...}]}}.

| Param | Type | Required | Description |
|---|---|---|---|
| `date_from` | integer | no |  |
| `date_to` | integer | no |  |
| `is_answered` | boolean | no |  |
| `skip` | integer | no |  |
| `take` | integer | no |  |
| `token` | string | yes |  |

### `wb_sales_recent`

_Policy op:_ `drive.read`

Recent sales+returns since `date_from`. saleID prefix: S=sale, R=return. Same flag semantics as wb_orders_recent.

| Param | Type | Required | Description |
|---|---|---|---|
| `date_from` | string | yes |  |
| `flag` | integer | no |  |
| `token` | string | yes |  |

### `wb_stocks_v2`

_Policy op:_ `drive.read`

WB FBO stocks snapshot via `/api/v1/supplier/stocks`. Returns full array (no pagination — WB returns one big response, up to ~50MB). Each row: {barcode, brand, category, lastChangeDate, quantity, quantityFull, Price, Discount, ...}.

| Param | Type | Required | Description |
|---|---|---|---|
| `date_from` | string | no | YYYY-MM-DD; defaults to today UTC. |
| `token` | string | yes |  |

### `wb_supplies_list`

_Policy op:_ `drive.read`

FBS supplies (заказы на отгрузку). `next_id` for pagination cursor.

| Param | Type | Required | Description |
|---|---|---|---|
| `limit` | integer | no |  |
| `next_id` | integer | no |  |
| `token` | string | yes |  |

### `wb_token_age`

_Policy op:_ `drive.read`

Decode a WB JWT (no signature verification, just claims) and return issued_at/expires_at/days_left/seller_id. Use to warn the user when a token is close to expiry.

| Param | Type | Required | Description |
|---|---|---|---|
| `token` | string | yes |  |

### `wb_warehouses`

_Policy op:_ `drive.read`

Official WB warehouse list (IDs you use in the marketplace API for FBS supplies). Returns [{id, name, address, ...}].

| Param | Type | Required | Description |
|---|---|---|---|
| `token` | string | yes |  |

---

## web

_Web fetch_ — 2 tools.

### `web_fetch`

_Policy op:_ `web.read`

Fetch a URL. mode='text' (default, extracts visible text), 'html', or 'json'. Cap 1 MB. Returns {content, _meta:{status_code, content_type, url_final, truncated}}.

| Param | Type | Required | Description |
|---|---|---|---|
| `mode` | string | no |  |
| `timeout` | number | no |  |
| `url` | string | yes |  |

### `web_search`

_Policy op:_ `web.read`

Web search via DuckDuckGo HTML (no API key). Returns {results: [{title, url, snippet}], _meta}. Best-effort — DDG HTML changes occasionally.

| Param | Type | Required | Description |
|---|---|---|---|
| `max_results` | integer | no |  |
| `query` | string | yes |  |

---

## webhook

_Webhook_ — 3 tools.

### `webhook_log`

_Policy op:_ `local.write`

Append an incoming webhook payload to the log. source: yookassa, tinkoff, telegram, wb_finance_notify, etc.

| Param | Type | Required | Description |
|---|---|---|---|
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `headers` | object | no |  |
| `payload` | object | yes |  |
| `signature_valid` | boolean | no |  |
| `source` | string | yes |  |

### `webhook_recent`

_Policy op:_ `local.read`

Recent webhooks, latest first.

| Param | Type | Required | Description |
|---|---|---|---|
| `limit` | integer | no |  |
| `source` | string | no |  |

### `webhook_verify_signature`

_Policy op:_ `local.read`

Verify HMAC-{algorithm} signature on a raw body. Used to validate ЮKassa / Tinkoff / WB callbacks.

| Param | Type | Required | Description |
|---|---|---|---|
| `algorithm` | string | no |  |
| `raw_body` | string | yes |  |
| `received_signature` | string | yes |  |
| `secret` | string | yes |  |

---

## yamarket

_Yamarket_ — 9 tools.

### `yamarket_businesses_list`

_Policy op:_ `drive.read`

List Yandex Market businesses (legal entities). `businessId` scopes products/inventory.

| Param | Type | Required | Description |
|---|---|---|---|
| `api_key` | string | yes |  |

### `yamarket_campaigns_list`

_Policy op:_ `drive.read`

List Yandex Market campaigns (shops) the api_key has access to. Returns {data:{campaigns:[{id, domain, business, ...}]}}. The `campaignId` is what every per-shop endpoint takes.

| Param | Type | Required | Description |
|---|---|---|---|
| `api_key` | string | yes |  |

### `yamarket_offers_list`

_Policy op:_ `drive.read`

Business-level offer catalog (across all campaigns).

| Param | Type | Required | Description |
|---|---|---|---|
| `api_key` | string | yes |  |
| `business_id` | integer | yes |  |
| `limit` | integer | no |  |
| `page_token` | string | no |  |

### `yamarket_order_get`

_Policy op:_ `drive.read`

Single order detail by id.

| Param | Type | Required | Description |
|---|---|---|---|
| `api_key` | string | yes |  |
| `campaign_id` | integer | yes |  |
| `order_id` | integer | yes |  |

### `yamarket_orders_list`

_Policy op:_ `drive.read`

Orders for a campaign. Dates DD-MM-YYYY (Yandex quirk). status: PROCESSING / DELIVERY / DELIVERED / CANCELLED / PICKUP.

| Param | Type | Required | Description |
|---|---|---|---|
| `api_key` | string | yes |  |
| `campaign_id` | integer | yes |  |
| `from_date` | string | yes |  |
| `page` | integer | no |  |
| `page_size` | integer | no |  |
| `status` | string | no |  |
| `to_date` | string | yes |  |

### `yamarket_prices_list`

_Policy op:_ `drive.read`

Current shop prices.

| Param | Type | Required | Description |
|---|---|---|---|
| `api_key` | string | yes |  |
| `campaign_id` | integer | yes |  |
| `limit` | integer | no |  |
| `page_token` | string | no |  |

### `yamarket_returns_list`

_Policy op:_ `drive.read`

Returns for a campaign. Dates DD-MM-YYYY.

| Param | Type | Required | Description |
|---|---|---|---|
| `api_key` | string | yes |  |
| `campaign_id` | integer | yes |  |
| `from_date` | string | yes |  |
| `limit` | integer | no |  |
| `page_token` | string | no |  |
| `to_date` | string | yes |  |

### `yamarket_stocks_list`

_Policy op:_ `drive.read`

Stocks for a campaign. Paginate via `paging.nextPageToken` returned in the response.

| Param | Type | Required | Description |
|---|---|---|---|
| `api_key` | string | yes |  |
| `campaign_id` | integer | yes |  |
| `limit` | integer | no |  |
| `page_token` | string | no |  |
| `with_turnover` | boolean | no |  |

### `yamarket_warehouses_list`

_Policy op:_ `drive.read`

Business warehouses.

| Param | Type | Required | Description |
|---|---|---|---|
| `api_key` | string | yes |  |
| `business_id` | integer | yes |  |

---

## yookassa

_Yookassa_ — 5 tools.

### `yookassa_payment_get`

_Policy op:_ `drive.read`

ЮKassa one payment by id.

| Param | Type | Required | Description |
|---|---|---|---|
| `payment_id` | string | yes |  |
| `secret` | string | yes |  |
| `shop_id` | string | yes |  |

### `yookassa_payments_list`

_Policy op:_ `drive.read`

List ЮKassa payments. status: pending, waiting_for_capture, succeeded, canceled.

| Param | Type | Required | Description |
|---|---|---|---|
| `created_gte` | string | no |  |
| `created_lte` | string | no |  |
| `cursor` | string | no |  |
| `limit` | integer | no |  |
| `secret` | string | yes |  |
| `shop_id` | string | yes |  |
| `status` | string | no |  |

### `yookassa_payouts_list`

_Policy op:_ `drive.read`

ЮKassa payouts (settlement money out).

| Param | Type | Required | Description |
|---|---|---|---|
| `limit` | integer | no |  |
| `secret` | string | yes |  |
| `shop_id` | string | yes |  |

### `yookassa_receipts_list`

_Policy op:_ `drive.read`

ЮKassa fiscal receipts (54-ФЗ).

| Param | Type | Required | Description |
|---|---|---|---|
| `created_gte` | string | no |  |
| `limit` | integer | no |  |
| `secret` | string | yes |  |
| `shop_id` | string | yes |  |

### `yookassa_refunds_list`

_Policy op:_ `drive.read`

ЮKassa refunds list.

| Param | Type | Required | Description |
|---|---|---|---|
| `created_gte` | string | no |  |
| `limit` | integer | no |  |
| `secret` | string | yes |  |
| `shop_id` | string | yes |  |

---

## zpl

_Zpl_ — 2 tools.

### `zpl_render_label`

_Policy op:_ `local.write`

Substitute {field} placeholders in a ZPL template and write to disk. Send the file to a Zebra ZPL printer.

| Param | Type | Required | Description |
|---|---|---|---|
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `fields` | object | yes |  |
| `out_path` | string | yes |  |
| `template` | string | yes |  |

### `zpl_render_wb_label`

_Policy op:_ `local.write`

Pre-baked WB FBS shipping label ZPL template — fills barcode + sku + supplier + weight, writes to disk.

| Param | Type | Required | Description |
|---|---|---|---|
| `barcode` | string | yes |  |
| `dry_run` | boolean | no | If true, do not execute. Return a preview describing what would happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive call's intent before let... |
| `out_path` | string | yes |  |
| `sku` | string | yes |  |
| `supplier` | string | yes |  |
| `weight_g` | integer | yes |  |

---
