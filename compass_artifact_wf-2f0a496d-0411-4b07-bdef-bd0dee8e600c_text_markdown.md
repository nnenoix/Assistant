# Аудит 236 инструментов Workspace Agent MCP-сервера — бэклог улучшений до идеала

## TL;DR

- **Главный архитектурный долг — отсутствие сквозной MCP-инфраструктуры**: tool annotations по MCP spec 2025-03-26 (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint` — введены в PR #185 Basil Hosmer, Anthropic), идемпотентные ключи в стиле Stripe `Idempotency-Key`, RFC 9457 problem+json envelope, единый retry-декоратор с экспоненциальной задержкой и jitter (формула Google: `min(((2^n)+random_number_milliseconds), maximum_backoff)`), dry-run/undo-log на все write-вызовы. До закрытия этих 5 слоёв точечные правки в инструментах дают ограниченный эффект.
- **Все 236 инструментов делятся на 4 риск-класса**: (R0) высокий blast radius — `sheets_update_*`, `drive_delete`, `drive_move`, `gmail_send`/`delete`, `apps_script_api_edit_file`/`create_deployment`, `wb_finance_detail_collect`, `calendar_delete/update`, `contacts_delete`; (R1) destructive но локально — local fs write, `notes_*`, `aliases_*`; (R2) idempotent write — `*_replace_text`, set-style, label-modify; (R3) read-only — большинство list/get/search/parse. Приоритет рефакторинга — строго R0 → R1 → R2 → R3.
- **Cognitive ergonomics для Claude Code**: жёсткий token budget на response (Anthropic в «Writing effective tools for agents», Sep 11 2025, явно требует «optimizing tool responses for token efficiency» через pagination, filtering, truncation; конкретное число «~25k» широко цитируется в комьюнити, но в самой статье не вынесено как нормативное — закрепляем как внутренний дефолт ≤25 000 tokens); обязательные `response_format: concise|detailed` enums на «толстых» tools (`sheets_query`, `gmail_search`, `drive_list`, `list_executions`, `wb_finance_detail_collect`); search-first вместо list-all — Anthropic verbatim: «In the address book case, you might choose to implement a search_contacts or message_contact tool instead of a list_contacts tool»; namespace-префикс категории через `_` на каждом из 236 имён.

---

## Key Findings

1. **MCP annotations покрытие — 0/236.** По спецификации MCP 2025-03-26 (PR #185) аннотации `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint` должны быть на КАЖДОМ tool. Без них любой compliant клиент обязан считать tool destructive (defaults: `destructiveHint=true, openWorldHint=true`) и показывать confirmation. Это ухудшает UX и блокирует параллелизацию read-only вызовов. VS Code Copilot и GitHub MCP уже используют аннотации для gate-конфирмаций.

2. **Идемпотентность write-вызовов отсутствует.** Stripe-style `Idempotency-Key` header — единственный надёжный способ безопасно ретраить POST к Google/WB/Apps Script. Stripe verbatim: «If the above Stripe request fails due to a network connection error, you can safely retry it with the same idempotency key, and the customer is charged only once». Сейчас retry `sheets_update` / `gmail_send` / `drive_copy` / `apps_script_api_edit_file` = риск дубликатов.

3. **Apps Script API критически опасен.** Эндпоинт `PUT https://script.googleapis.com/v1/projects/{scriptId}/content` — полный PUT; verbatim из официальной документации: «This clears all the existing files in the project». PATCH-эндпоинта нет. Существующее локальное staging — хорошо, но требует pre-edit backup всех файлов через `projects.getContent` + создание `projects.versions.create` snapshot для rollback. `scripts.run` требует совпадения Cloud Project между caller и скриптом — Google verbatim: «The error 403, PERMISSION_DENIED: The caller does not have permission indicates that the Cloud Platform project used to authorize the request is not the same as the one used by the script». Apps Script API не работает с service accounts — verbatim: «The Apps Script API doesn't work with service accounts».

4. **WB API rate-limits игнорируются.** WB использует token bucket; возвращает headers `X-Ratelimit-Limit`, `X-Ratelimit-Remaining`, `X-Ratelimit-Reset`, `X-Ratelimit-Retry` (verbatim из dev.wildberries.ru/openapi/api-information) — их надо парсить и в Retry-After-стиле задерживать. `wb_finance_detail_collect` обращается к `https://statistics-api.wildberries.ru/api/v5/supplier/reportDetailByPeriod` с лимитом 80 000 строк per response (verbatim из WB docs: «For a single response to a request with flag=0 or without flag, a conditional limit of 80,000 rows is set»). Без incremental retry по `lastChangeDate` курсору цепочка ломается.

5. **Google quotas конкретны и одинаковы по паттерну, но цифры разные.** Sheets: 300 read RPM/project (verbatim: «there's a read request limit of 300 per minute per project»); Drive: 12 000 RPM/project default; Gmail: **1 200 000 quota units/min per project + 6 000 units/min per user per project** для проектов, созданных после 1 мая 2026 (verbatim из Google docs и Nylas CLI guide). Стоимость методов Gmail: `messages.send` = 100 units, `messages.get` = **20 units** (Google docs / Nylas CLI: «messages.list at 5 units, history.list at 2, messages.get at 20, threads.get at 40, and messages.send at 100 units per request»). Все требуют формулу `min((2^n)*1000+rand_ms_≤1000, 64000)`, до 5–7 попыток, ретрай только {429, 500, 502, 503, 504} — не 4xx кроме 429.

6. **Token efficiency — главный затык по эргономике.** Anthropic engineering: «Optimizing tool responses for token efficiency» — pagination, filtering, truncation с sensible defaults. Это особенно критично для `sheets_get_values` (целые листы), `gmail_get` (полные тела с attachments), `drive_list` (1000 файлов через nextPageToken), `apps_script list_executions`, `watcher_recent_failures`, `wb_finance_detail_collect`.

7. **Error model унифицирован «как попало».** Должен быть один envelope, совместимый с RFC 9457 (заменил RFC 7807): `{type, title, status, detail, instance, retriable, retry_after_ms, idempotency_key, fix_hint}`. Media type `application/problem+json`. Это даёт Claude actionable error и автоматический retry policy.

8. **OWASP MCP Top 10 — Tool Poisoning и косвенный Prompt Injection.** Уточнение: в самом OWASP MCP Top 10 v0.1 (project lead Vandana Verma Sehgal) **MCP01:2025 — это «Token Mismanagement & Secret Exposure», а Prompt Injection via Contextual Payloads — MCP06:2025**. (Prompt Injection — это LLM01:2025 в отдельном OWASP LLM Top 10.) Любой read-only tool, который тянет внешний контент (`gmail_get` с письмом от внешнего отправителя, `drive_download` .docx из shared folder, `web_fetch`, `chat_history_*`, gmail attachments, `vision_ocr`), должен помечать результат как `untrusted_content=true`. Иначе атакующий через входящее письмо может скомандовать агенту запустить `drive_delete` или утечь WB-токен через `web_fetch`.

9. **«Lethal trifecta» риск (Simon Willison, June 2025).** В наборе уже есть три ноги: (1) private data — Sheets/Drive/Gmail/WB; (2) untrusted content — `web_fetch`, gmail входящие, drive shared; (3) exfiltration — `gmail_send`, `web_fetch` (POST), `drive_share`. Любая сессия, где все три tool-семейства активны, требует human-in-the-loop confirmation на любой destructive call. Реализовать как session-level taint tracking и блокировку `gmail_send`/`drive_share`/`web_fetch` после первого tainted read.

10. **Поведение defaults — silent None vs phantom-data.** Многие tools принимают опциональные параметры со значением None; Claude иногда передаёт строку «null» или пустую строку. Известный конфликт Pydantic strict mode + Claude Code (issue anthropics/claude-code#3084) — Claude сериализует nested objects как JSON-строки; нужны flat schemas или явный JSON-string-unwrap-декоратор. Использовать `model_config = ConfigDict(strict=True)` + явные defaults вида phantom-data (например, для `range="A1:Z"` — explicit «whole sheet, but capped at 10k rows»).

---

## Общие принципы улучшений (cross-cutting, применимы ко всем 35 категориям)

Эти 18 принципов — обязательная инфраструктура. Без них точечные правки tool-by-tool не имеют смысла. Опираемся на: Anthropic «Writing effective tools for agents» (Sep 11, 2025), MCP spec 2025-03-26, Stripe idempotency design, RFC 9457, Google API quota guides, OWASP MCP Top 10 v0.1.

| # | Принцип | Обоснование / якорь | Применимость |
|---|---|---|---|
| 1 | **MCP annotations на все 236 tools** (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`, `title`) | MCP spec 2025-03-26 PR #185 (Basil Hosmer, Anthropic). Conservative defaults бьют по UX. | Все |
| 2 | **Idempotency-Key для всех write-вызовов** (UUIDv4, scope=tool+user+args-hash, TTL 24h, sqlite store) | Stripe blog «Designing robust and predictable APIs with idempotency». | Все P0 destructive write |
| 3 | **Единый retry-декоратор** (`min((2^n*1000)+random_ms_≤1000, 64000)`, n≤5, retry on {429, 500, 502, 503, 504}, parse `Retry-After`/`X-Ratelimit-Reset`) | Google verbatim formula. Не ретраить 4xx кроме 429. | Все external API |
| 4 | **RFC 9457 problem+json envelope** на все ошибки: `{type, title, status, detail, instance, retriable, retry_after_ms, fix_hint, idempotency_key}`, media type `application/problem+json` | RFC 9457 (заменил 7807). | Все |
| 5 | **Response token budget ≤25k токенов с явным truncation** (head/tail/summary стратегии + `truncated: true, full_size: N` маркер) | Anthropic: «Optimizing tool responses for token efficiency». 25k — внутренний дефолт, выбран как разумный sub-context-window threshold. | Все list/get/search |
| 6 | **`response_format: concise|detailed` enum** на всех «толстых» tools с дефолтом concise | Anthropic engineering recommendation на response-format параметр. | sheets/gmail/drive/calendar/list_executions |
| 7 | **Strict input validation через Pydantic strict mode** (`ConfigDict(strict=True)`) + `input_examples` поле | Anthropic Claude API docs про `input_examples` (~20-50 tokens для simple, ~100-200 для complex) — окупается резким падением malformed-call rate. | Все |
| 8 | **Namespace-префикс категории через `_`** (snake_case, ≤56 символов из-за FastMCP лимита; см. FastMCP issue #2596) | Anthropic verbatim: «Prefix tools with their domain for clarity and scalability». | Все 236 |
| 9 | **Search-first вместо list-all** для всех потенциально больших коллекций | Anthropic verbatim: «In the address book case, you might choose to implement a search_contacts or message_contact tool instead of a list_contacts tool». | Gmail/Drive/Contacts/Sheets |
| 10 | **Dry-run mode на все destructive tools** (`dry_run: bool = False` → возвращает план изменений без выполнения) | OWASP MCP Top 10 + LangGraph human-in-the-loop interrupt pattern. | drive_delete, sheets_update_*, gmail_send, wb_*, calendar_delete, apps_script_*_edit |
| 11 | **Undo log** (sqlite WAL, last 100 destructive ops, prev_value, etag, version_id) | Apps Script `updateContent` не имеет undo — обязаны хранить snapshot. | Все destructive |
| 12 | **Blast radius limits как параметры с защитой** (`max_rows_affected: int=100`, `max_files_deleted: int=10`, `confirm_token` для >threshold) | OWASP: «Limit what agents can request of peers, and scope high-privilege agents». | drive_delete, sheets_*_clear, gmail_batch_delete |
| 13 | **`openWorldHint=true` ⇒ tainted result marker** (`source: untrusted, content_safety: needs_review`) — блокировка destructive-tools после первого tainted read в сессии | Simon Willison «lethal trifecta» (June 2025): private data + untrusted content + exfiltration. | gmail_get, drive_download (shared), web_fetch, chat_history, vision_ocr, contacts_search |
| 14 | **ETag/version-aware updates** (если API поддерживает: Drive, Calendar, Contacts — обязательно `If-Match`/`metadata.sources.etag`) | Google People API verbatim: «The server returns a 400 error with reason 'failedPrecondition' if person.metadata.sources.etag is different than the contact's etag». | drive_*, calendar_*, contacts_* |
| 15 | **Batch-first вместо loop** через native batch endpoints | Google Drive verbatim: «You're limited to 100 calls in a single batch request». People API batch ≤200. Снижает квоту в 100×. | Sheets/Drive/Gmail/Contacts |
| 16 | **Cache layer для read-only с TTL** (sqlite + content hash) | Google guidance на caching для quota reduction. | Все read-only, особенно metadata |
| 17 | **Streaming/incremental cursor pagination** с явным `next_page_token` в response (без silent-truncation) | WB API verbatim: «more than one request may be necessary … use the full value of the lastChangeDate field of the last row». Google Drive `nextPageToken` идентичен. | drive_list, gmail_list, sheets_query, wb_finance_detail_collect, list_executions, chat_history_* |
| 18 | **Telemetry & audit log** (immutable JSON-lines: tool, ts, user_alias, args_hash, idempotency_key, status, duration_ms, blast_radius) | OWASP MCP Top 10 (MCP08 «Insufficient Telemetry» — конкретный risk class в v0.1). | Все, обязательно destructive |

---

## Прохождение по всем 35 категориям

Условные обозначения: **P0** — critical, делать в ближайший спринт. **P1** — важно, до месяца. **P2** — улучшение, по возможности. Оси: **R**eliability, **E**rgonomics, **P**erformance, **S**ecurity.

### 1. Aliases (4 tools)
**Характеристика:** локальный реестр name→Google account. Чисто local fs/state, без сети. **Риск:** случайное затирание binding ⇒ агент пишет не в тот аккаунт ⇒ blast radius огромный.
**Категорийные improvements:** ETag-стиль через `expected_current_target` в `aliases_add`; `aliases_remove` требует двухфазного confirm если alias использовался последние 24h. Persistent journal с rollback. MCP `openWorldHint=false` для всех 4.

| Tool | Проблема/риск | Improvement | Приор. | Ось |
|---|---|---|---|---|
| aliases_add | Перезаписывает существующий без warning | Возврат `was_overwrite: bool, previous_target` + опц. `overwrite: false` (default), требовать explicit `overwrite=true` | P0 | R,S |
| aliases_list | None — read-only | annotations: readOnly=true, idempotent=true, openWorld=false; кэш 60s | P2 | E,P |
| aliases_remove | Удаление alias может оставить «дыру» в работающих pipelines | `dry_run`, проверка last-use (если использовался <24h — требовать `force=true`), undo log | P0 | R,S |
| aliases_resolve | None — read-only, но критичный для всех downstream | annotations + кэш в памяти на время сессии; error envelope с `fix_hint: "use aliases_list to see valid names"` | P1 | R,E |

### 2. Analytics (2 tools)
**Характеристика:** локальный ABC анализ. Compute-only, без сети.
**Категорийные improvements:** explicit input schema с проверкой типов столбцов; параметризовать пороги (по умолчанию 80/15/5); возвращать summary + первые/последние 20 строк, а не весь массив.

| Tool | Проблема | Improvement | Приор. | Ось |
|---|---|---|---|---|
| analytics_abc | На больших массивах возвращает 10k+ строк в context | `response_format` enum; truncation head/tail/summary; явное предупреждение если N>1000 | P1 | E,P |
| analytics_abc_split | Дубликат логики; неясно когда применять | Docstring «use analytics_abc when у вас 3 метрики (revenue/qty/margin); analytics_abc_split — когда одна метрика» — explicit when-to-use guidance | P1 | E |

### 3. Apps Script (27 tools) — **R0 КРИТИЧЕСКАЯ**
**Характеристика:** мощный, опасный, единственный путь редактировать GAS-проекты. `projects.updateContent` — full PUT, который verbatim «clears all the existing files in the project»; нет PATCH. **Риск:** ошибка в манифесте → проект не сохраняется; пропущенный файл → потеря кода; неправильный scope → broken deployments; service account не работает с Apps Script API.
**Категорийные improvements:**
- Обязательный pre-edit `projects.getContent` snapshot → diff → preview → confirm → write.
- Версионирование: каждое мутирующее действие должно сначала создавать `projects.versions.create` (immutable snapshot), и только потом `updateContent`. Это native rollback.
- Manifest validation локально (JSON schema для `appsscript.json`) до отправки.
- Cloud project mismatch detection для `scripts.run` (caller OAuth client и script должны делить standard Cloud project).
- Все edit-tools требуют идемпотентного ключа = `sha256(files_array_canonical_json)`.
- Runtime квоты НЕ применимы к API surface; ограничения runtime (6 мин per invocation, 20k UrlFetch/day consumer / 100k Workspace, 90 мин trigger/day) — для самого исполнения, но `scripts.run` тоже их наследует.

| Tool | Проблема/риск | Improvement | Приор. | Ось |
|---|---|---|---|---|
| apps_script_api_create_project | Дубликаты, отсутствие undo | Idempotency-Key, возврат `web_url` + `script_id` в едином envelope, annotations: destructive=false, idempotent=false | P1 | R,S |
| apps_script_api_get_content | Большой ответ (десятки файлов × kB) | `response_format`, фильтр по `file_names: list[str]`, `include_source: bool=true`; кэш по script_id+last_modified | P0 | E,P |
| apps_script_api_get_file | OK по объёму, но dup с get_content | Явный docstring «used when you already know exact file name; иначе get_content». MCP readOnly=true | P2 | E |
| apps_script_api_list_files | None | annotations readOnly=true; кэш | P2 | E,P |
| apps_script_api_edit_file | **HIGH BLAST**: full replace, может зачистить manifest | Pre-edit: getContent → backup → manifest validation → версия snapshot → updateContent. `dry_run` показывает unified diff. После — `verify_after_edit` — повторный get + hash check. annotations destructive=true, idempotent=true (если same content) | P0 | R,S |
| apps_script_api_replace_function | Regex-based замена в .gs — рискованно | AST-aware замена через GAS parser (или ограничить: только функции верхнего уровня по regex `^function\\s+NAME\\s*\\(`); dry_run обязателен; верифицировать, что после замены файл всё ещё валидный JS-подмножество | P0 | R |
| apps_script_api_create_version | OK | Idempotency-Key по script_id+description+ts_bucket(60s); annotations destructive=false | P1 | R |
| apps_script_api_list_versions | None | readOnly, кэш 5 мин | P2 | E,P |
| apps_script_api_create_deployment | Может опубликовать сломанный код | Pre-check: HEAD passes static syntax check; require explicit `version_number` (no «latest»); annotations destructive=true (replaces live) | P0 | R,S |
| apps_script_api_list_deployments | None | readOnly | P2 | E |
| apps_script_api_update_deployment | Меняет live URL/version | dry_run, version diff в preview, idempotency-key | P0 | R,S |
| apps_script_api_delete_deployment | Ломает live web app | Require `confirm_url_pattern` совпадение с реальной deployment URL; undo log | P0 | S |
| apps_script_api_run_function | Cloud project mismatch → 403 «The caller does not have permission» | Pre-flight: parse deployment_id → check Cloud project match → if devMode=true, check current user is script owner; вернуть fix_hint при 403 | P0 | R,E |
| apps_script_api_get_execution_logs | Большой ответ | Truncation; `since: timestamp`; pagination | P1 | E,P |
| apps_script_api_list_executions | До 1000 записей | Pagination, фильтр по status/function_name, `response_format` | P0 | E,P |
| apps_script_api_get_execution | OK | readOnly | P2 | — |
| apps_script_api_get_metrics | OK | readOnly, кэш 10 мин | P2 | E |
| apps_script_api_get_processes | OK | readOnly, фильтр active | P2 | E |
| apps_script_find_bound_script | По имени Sheet/Doc может вернуть несколько | Возвращать список с `confidence_score`, не первый match | P1 | R,E |
| apps_script_get_bound_script_token | Token в логи — leak | Mask в логах, TTL ≤ 1h, audit на каждый retrieve | P0 | S |
| apps_script_register_bound_script | Локальный journal — risk перезаписи | Append-only журнал с conflict detection | P1 | R |
| apps_script_resolve_script_id | OK | readOnly, кэш | P2 | E |
| apps_script_stage_file | Локально работает — это правильный паттерн | Расширить: staging должен быть mandatory before edit_file (нельзя edit без stage) | P0 | R,S |
| apps_script_get_staging_diff | OK | readOnly | P2 | — |
| apps_script_commit_staging | Должен быть единственный путь к updateContent | Force: edit_file / replace_function должны под капотом всегда проходить через staging | P0 | R,S |
| apps_script_revert_staging | OK | annotations destructive=false | P2 | — |
| apps_script_list_staged | OK | readOnly | P2 | — |

### 4. Auth / Accounts (6 tools)
**Характеристика:** OAuth alias-аккаунты Google. **Риск:** token leak, refresh failure, кросс-аккаунт write. По OWASP MCP Top 10 v0.1 это MCP01 — «Token Mismanagement & Secret Exposure» — top-risk #1.
**Категорийные improvements:** все tokens только из keychain (никогда не file plain), audit log каждого retrieve, mask в любых return values; разделить read-only metadata (account email, scopes, expiry) от sensitive (token).

| Tool | Проблема | Improvement | Приор. | Ось |
|---|---|---|---|---|
| auth_add_account | OAuth flow может оставить полупустой alias | Atomic write: либо весь flow до refresh_token завершился, либо ничего не сохранено; idempotency по email+scopes | P0 | R,S |
| auth_list_accounts | Может выдать токены в payload | Никогда не возвращать access_token/refresh_token; только email, scopes_summary, expires_at, last_used | P0 | S |
| auth_remove_account | Удаляет refresh_token без отзыва на Google side | После delete локально — попытаться revoke на oauth2.googleapis.com/revoke; undo log с возможностью повторной авторизации | P1 | S |
| auth_check_token | OK | readOnly, не возвращать сам token | P1 | S |
| auth_refresh_token | Race condition при параллельных вызовах | file-lock per alias; idempotent | P0 | R |
| auth_get_scopes | OK | readOnly, кэш | P2 | E,P |

### 5. Bank statement parsers (3 tools)
**Характеристика:** парсинг выписок Точка/Альфа/Сбер. Local compute, без сети. **Риск:** парсер падает на новом формате → silent data loss; OCR-fallback может галлюцинировать суммы.

| Tool | Проблема | Improvement | Приор. | Ось |
|---|---|---|---|---|
| bank_parse_tochka | Хрупкость к смене формата | Версия формата в payload; reject если не распознан known signature; sample-tests как regression | P1 | R |
| bank_parse_alfa | То же | Аналогично | P1 | R |
| bank_parse_sber | То же; PDF-based — Tesseract под капотом | Preprocessing: 300 DPI, deskew, denoise (cv2). Confidence score per row; reject row если confidence < 0.85; явный `untrusted_content` маркер | P0 | R,S |

### 6. Browser automation Playwright (5 tools)
**Характеристика:** headless. **Риск:** flaky, leak credentials, prompt injection через DOM, infinite hangs.
**Категорийные improvements:** Все calls с явным `timeout` (default 30s; для navigation 60s); auto-screenshot on failure; никогда не передавать в payload user secrets; `openWorldHint=true`, `destructiveHint=true` (form submit может быть destructive); обязательная нормализация tainted output.

| Tool | Проблема | Improvement | Приор. | Ось |
|---|---|---|---|---|
| browser_open | Утечка sessionStorage между задачами | Свежий incognito context на каждый вызов; явный `persist_session: bool=False` | P0 | S |
| browser_click | Race conditions, flaky | Заменить hard waits на `expect(locator).toBeVisible()`; auto-wait built-in Playwright; retry на StaleElement до 3 раз | P1 | R |
| browser_fill | Нет dry-run; пароли в логах | Mask поля где name содержит pass/secret/token; dry-run возвращает план | P0 | S |
| browser_screenshot | Большие PNG в context | Авто-resize до 1280×720, jpeg quality 70, max 200 KB; полный — opt-in | P1 | E,P |
| browser_evaluate | RCE-эквивалент в браузере; XSS-исполнение чужого JS | Запретить (или whitelist read-only функций); если оставить — annotations destructive=true, явный consent | P0 | S |

### 7. Bulk payloads (1 tool)
| bulk_payload_load | Чтение произвольного файла — path traversal | Whitelist директорий (рабочая, /tmp); max size 50 MB; sniff JSON/CSV/text; refuse executables | P0 | S |

### 8. Google Calendar (12 tools)
**Характеристика:** read/write events; основной риск — duplicate event при retry, неверный TZ, удаление чужого события.
**Категорийные improvements:** все create/update требуют client-generated event ID (Google verbatim: «you can choose to generate your own event ID that conforms to our format requirements») → нативная идемпотентность; ETag/sequence на update для optimistic concurrency; `sendUpdates` всегда explicit (`none`/`externalOnly`/`all`).

| Tool | Проблема | Improvement | Приор. | Ось |
|---|---|---|---|---|
| calendar_list_events | До 250 events × big payload | response_format, фильтры (eventType, q), pagination по syncToken, кэш на 60s | P0 | E,P |
| calendar_create_event | Дубликаты при retry | Mandatory client-generated event ID = `sha256(args)` (urlsafe ≤1024 char); idempotency-key; annotations destructive=false, idempotent=true | P0 | R,S |
| calendar_update_event | Перезатирание чужих изменений | Read-modify-write через ETag/sequence; reject на mismatch с fix_hint «refetch and merge» | P0 | R |
| calendar_delete_event | Безвозвратно | dry_run, undo log (хранить snapshot 30 дней), require sendUpdates explicit | P0 | S |
| calendar_suggest_time | OK | readOnly, кэш busy-windows на 5 мин | P2 | E,P |
| calendar_list_calendars | OK | readOnly, кэш 1h | P2 | E,P |
| calendar_respond_to_event | OK | idempotency, undo (revert на previous response) | P1 | R |
| calendar_get_event | OK | readOnly, кэш 30s | P2 | E |
| calendar_quick_add | Парсинг natural language — может промахнуться | Возврат preview перед commit; dry_run по умолчанию | P1 | R |
| calendar_move_event | Меняет organizer | dry_run, undo | P0 | R,S |
| calendar_watch | Webhook expiry → silent failure | Авто-renewal по `expiration` field; alerting | P1 | R |
| calendar_import_event | Только default type | Pre-check eventType; явный error если non-default | P1 | R |

### 9. Chat history (4 tools)
**Характеристика:** read-only, но **tainted content** (внешние пользователи). **Риск:** prompt injection (MCP06:2025 «Prompt Injection via Contextual Payloads» в OWASP MCP Top 10 v0.1).
**Категорийные improvements:** все 4 возвращают `untrusted_content=true`; truncation + summary; redact phone/email/cards default (opt-in `redact=false`).

| Tool | Проблема | Improvement | Приор. | Ось |
|---|---|---|---|---|
| chat_history_list | Объём | Pagination, `response_format`, фильтры по дате/чату | P0 | E,P |
| chat_history_search | Хорошее API-form (search-first) — поощряем | `limit`, `since`, кэш | P1 | E |
| chat_history_get | Большой ответ | head/tail truncation, attachments только metadata | P0 | E |
| chat_history_export | Может слить весь архив | Hard cap (default 1000 msg), confirm на >cap | P1 | S |

### 10. Cloud Logging (2 tools)
| logging_query | Большой ответ | filter обязателен, time-range mandatory, pagination | P0 | E,P |
| logging_tail | Может зависнуть | timeout, max_lines cap | P1 | R |

### 11. Google Contacts (5 tools)
**Категорийные improvements:** все mutate sequential per user (Google verbatim: «Mutate requests for the same user should be sent sequentially to avoid increased latency and failures»); ETag mandatory на update/delete; batch endpoint ≤200; warmup-query для search (Google verbatim: «IMPORTANT: Before searching, clients should send a warmup request with an empty query to update the cache»).

| Tool | Проблема | Improvement | Приор. | Ось |
|---|---|---|---|---|
| contacts_list | Большой ответ | pageSize cap 200, response_format, `personFields` whitelist обязателен | P0 | E,P |
| contacts_search | Cache cold start needs warmup | Авто-warmup empty-query перед первым search; кэш 5 мин | P0 | R,E |
| contacts_create | Дубликаты | Idempotency по `emailAddresses[0].value + names`; batch до 200 (`people:batchCreateContacts`) | P1 | R,P |
| contacts_update | ETag mismatch → 400 `failedPrecondition` | Read latest → merge → write; auto-retry max 1 на mismatch с refresh; `updateMask` mandatory | P0 | R |
| contacts_delete | Безвозвратно | dry_run, undo log (хранить full Person 30 дней), batch endpoint | P0 | S |

### 12. Google Docs (6 tools)
**Категорийные improvements:** `batchUpdate` первичен; индексы хрупки между вызовами → всегда get+batch в одной транзакции.

| Tool | Проблема | Improvement | Приор. | Ось |
|---|---|---|---|---|
| docs_create | Дубликаты | Idempotency-Key + явное `parent_folder_id`; вернуть `document_id`, `web_url` | P1 | R |
| docs_get | Большой ответ | `response_format` (concise=plain text + structure; detailed=full Document), section filter | P0 | E,P |
| docs_batch_update | Индексы устаревают | Mandatory read-then-update в одном вызове; idempotency на operations | P0 | R |
| docs_copy | OK | Idempotency, явный destination | P1 | R |
| docs_append | Race с другим editor | Append-only через end-of-doc index + retry на conflict | P1 | R |
| docs_replace_text | Может зацепить лишнее | dry_run возвращает все matches с context; require `match_case` explicit; max_replacements cap | P0 | R,S |

### 13. Google Drive (25 tools) — **R0 для delete/move/share**
**Характеристика:** многоаккаунтный fan-out `*` — мощно и опасно.
**Категорийные improvements:**
- `fan_out="*"` — обязательно требует `confirm_all_accounts=true`; иначе reject с понятным error.
- Batch endpoint (`/batch/drive/v3`, verbatim лимит «100 calls in a single batch request») на любые ≥3 операции.
- `fields` whitelist mandatory во всех list/get — иначе Drive вернёт всё (>kB на файл).
- Daily upload cap (Google verbatim: «Google Workspace users can only upload 750 GB per day between My Drive and all shared drives») — добавить proactive check в upload. Max file size 5 TB, max copy 750 GB.
- `supportsAllDrives=true` обязательно если есть shared drives в scope.
- ETag / `headRevisionId` для версионности.
- Default pageSize 100, max 1000 (Google docs паттерн).

| Tool | Проблема | Improvement | Приор. | Ось |
|---|---|---|---|---|
| drive_list | Может вернуть 1000 файлов × heavy metadata | Default pageSize=50, max=200; mandatory `fields`; `q` filter mandatory если parent неуказан | P0 | E,P |
| drive_search | Хорошо named | Кэш 60s на одинаковый запрос | P1 | E,P |
| drive_get_metadata | Объём metadata | `fields` mandatory | P1 | E,P |
| drive_download | Может скачать GB | max_size param (default 50 MB), stream + truncation, mime-aware | P0 | R,P |
| drive_upload | Дубликаты, 750 GB cap | Idempotency-Key, pre-check daily quota; resumable upload для >5 MB | P0 | R,S |
| drive_copy | Дубликаты | Idempotency, `appProperties.copy_key` для дедупликации | P0 | R |
| drive_move | Меняет parents — может ломать sharing | dry_run, list текущих parents в response, undo log | P0 | R,S |
| drive_rename | OK | Idempotency, ETag | P1 | R |
| drive_delete | **БЕЗВОЗВРАТНО** или в trash | Default: trash (revertable 30 дней); `permanent=true` требует explicit confirm; dry_run; max_files cap | P0 | S |
| drive_share | Утечка данных | dry_run показывает все granted permissions; require explicit `notify=false` чтобы не спамить; whitelist domains | P0 | S |
| drive_permissions_list | OK | readOnly | P2 | E |
| drive_permissions_add | См. drive_share | то же | P0 | S |
| drive_permissions_remove | OK | undo (recreate permission), idempotency | P1 | R |
| drive_permissions_update | Role change | dry_run | P0 | S |
| drive_create_folder | Дубликаты | Idempotency по name+parent | P1 | R |
| drive_get_folder_id | OK | Кэш 1h | P2 | E,P |
| drive_list_folders | OK | response_format, pagination | P1 | E,P |
| drive_get_file_revisions | OK | readOnly | P2 | E |
| drive_restore_revision | Risky | dry_run, явный revision_id | P0 | S |
| drive_empty_trash | **DESTRUCTIVE permanent** | Confirm с listing файлов; max_count cap; undo невозможен — двухступенчатый confirm | P0 | S |
| drive_create_shortcut | OK | Idempotency | P2 | R |
| drive_fan_out_query | `*` без guard | Mandatory `confirm_all_accounts`; список аккаунтов в response; per-account error envelope | P0 | R,S |
| drive_export | Может вернуть много | response_format, mime negotiation | P1 | E,P |
| drive_get_about | OK | readOnly, кэш 1h | P2 | E,P |
| drive_change_owner | Меняет ownership | dry_run, undo через permissions_update | P0 | S |

### 14. Excel xlsx local (1 tool)
| excel_parse | Большой файл → OOM | `max_rows`, `sheet_name` mandatory если файл с >1 sheet, `response_format` (concise=headers+first 100 rows + counts) | P0 | E,P,R |

### 15. File analyze/extract (5 tools)
| file_info | OK | readOnly, проверка пути (path traversal) | P1 | S |
| image_dims | OK | readOnly | P2 | — |
| file_hash | OK | readOnly, кэш | P2 | E,P |
| file_extract_text | Может тянуть GB | max_size cap, truncation | P1 | E,P |
| file_detect_mime | OK | readOnly, кэш | P2 | E |

### 16. Google Forms (4 tools)
| forms_create | Дубликаты | Idempotency | P1 | R |
| forms_get | OK | response_format | P1 | E |
| forms_list_responses | Может быть много | pagination, since-timestamp | P0 | E,P |
| forms_update | Schema mismatch | dry_run, validation | P1 | R |

### 17. Currency / FX (1 tool)
| fx_rate | Сетевая зависимость, может молча отдать stale | Возвращать `as_of: timestamp`, `source`, `confidence`; кэш 1h; fallback на cached если live недоступен с explicit `stale=true` | P0 | R,E |

### 18. GCP project management (4 tools)
| gcp_list_projects | OK | readOnly, кэш 5 мин | P2 | E |
| gcp_set_project | Race condition (глобальный state) | Per-session state, не глобальный | P0 | R |
| gcp_get_project | OK | readOnly | P2 | — |
| gcp_check_apis_enabled | OK | readOnly, кэш | P2 | E,P |

### 19. Gmail (17 tools) — **R0 для send/delete**
**Категорийные improvements:** Gmail quota units (verbatim из Google docs / Nylas CLI guide: `messages.list`=5, `history.list`=2, `messages.get`=20, `threads.get`=40, `messages.send`=100); project cap 1 200 000 units/min + per-user 6 000 units/min для проектов после 1 мая 2026. Приоритет batch + history-based incremental; client-generated Message-ID для идемпотентности send; partial responses (`fields=`) и `Accept-Encoding: gzip` обязательны. Concurrent request limit per user — 50.

| Tool | Проблема | Improvement | Приор. | Ось |
|---|---|---|---|---|
| gmail_list | Большой ответ; consumes много units | `maxResults` cap 50; `q` mandatory или explicit `confirm_full_inbox=true`; partial response через `fields` | P0 | E,P |
| gmail_search | OK | `q` validation (Google query syntax); кэш 30s | P1 | E,P |
| gmail_get | Полный raw — мегабайты, дорогой (20 units) | response_format: concise=headers+plain text body / detailed=full raw; attachments только metadata по умолчанию | P0 | E,P |
| gmail_get_attachment | Большой | max_size cap, mime check | P1 | P,S |
| gmail_send | **HIGH BLAST** — спам, утечка, 100 units | Mandatory client-generated Message-ID; idempotency; dry_run возвращает full preview; recipients allowlist опц.; require explicit `confirm_recipients=true` для >5; rate limit per session | P0 | R,S |
| gmail_draft_create | Дубликаты | Idempotency-Key | P1 | R |
| gmail_draft_update | ETag/historyId | optimistic concurrency | P1 | R |
| gmail_draft_send | См. gmail_send | то же | P0 | R,S |
| gmail_modify_labels | Atomically wrong scope | `batchModify` endpoint; idempotency по (msg_id, label_set) | P0 | R,P |
| gmail_trash | OK (revertable) | annotations destructive=false, idempotent=true | P2 | — |
| gmail_untrash | OK | то же | P2 | — |
| gmail_delete | **БЕЗВОЗВРАТНО** | trash-first паттерн; permanent=true требует confirm; dry_run; undo log | P0 | S |
| gmail_batch_delete | Большой blast | Hard cap 100; preview list; confirm | P0 | S |
| gmail_threads_list | OK | pagination, response_format | P1 | E,P |
| gmail_threads_get | Большой, 40 units | response_format | P1 | E,P |
| gmail_history_list | OK — нативная инкр. синхр., 2 units | Поощряем — предпочитать вместо list если есть historyId | P1 | P |
| gmail_watch | Webhook expire | auto-renewal | P1 | R |

### 20. Local filesystem (6 tools)
**Категорийные improvements:** path-traversal whitelist (workspace root, /tmp); max sizes; никаких symlink-follow вне whitelist; `openWorldHint=false`.

| Tool | Проблема | Improvement | Приор. | Ось |
|---|---|---|---|---|
| fs_read_file | offset/limit уже есть — хорошо | Строгий strict-mode для offset/limit (negative reject); max bytes cap | P1 | R,S |
| fs_write_file | Перезатирание | `mode: write|append|create_new`; atomic write через temp+rename; backup .bak | P0 | R,S |
| fs_list_dir | recursive может взорваться | `max_depth: int=2`, `max_entries: int=500`, exclude patterns; response_format | P0 | E,P |
| fs_mkdir | OK | Idempotency (parents=true ок) | P2 | R |
| fs_delete | **DESTRUCTIVE** | trash-first (mv to .trash/), dry_run, max_files cap, undo | P0 | S |
| fs_move | OK | atomic, undo log | P1 | R,S |

### 21. Agent notes (5 tools)
**Категорийные improvements:** notes — это agent memory, требует ACID writes + history.

| note_add | Дубликаты | Idempotency по hash(content+tags) | P1 | R |
| note_get | OK | readOnly, кэш | P2 | E |
| note_search | OK | full-text + tags filter, ranked | P1 | E |
| note_update | Race | Per-note version; optimistic | P1 | R |
| note_delete | Безвозвратно | trash-first; retain 30 дней | P0 | S |

### 22. Open external app (1 tool)
| open | Может запустить произвольный binary | Whitelist apps; refuse executables по path; `dry_run` | P0 | S |

### 23. PDF generation (1 tool)
| pdf_create | Дубликаты, большие файлы | Idempotency; max page count; warn если >10MB | P1 | R,P |

### 24. Reply lint (1 tool)
| reply_lint | Сам по себе guardrail, но может быть обойдён | Обязательно вызывать ДО любого outbound (gmail_send, drive_share notify). Сделать pre-hook в самом сервере, чтобы не было «забыл вызвать» | P0 | S |

### 25. Reports (5 tools)
| report_create | Дубликаты | Idempotency-Key | P1 | R |
| report_render | Большой output | streaming в файл, не в context | P1 | E,P |
| report_get | OK | response_format | P1 | E |
| report_list | OK | pagination | P2 | E,P |
| report_delete | OK | trash-first, undo | P1 | S |

### 26. Self-heal / introspection (9 tools)
**Категорийные improvements:** self-tools должны быть полностью статически описаны, без зависимостей; кэшировать схему.

| self_diagnose | OK | structured output | P2 | E |
| list_tools | До 236 имён в context | Categorized output с counts + opt-in expand; кэш | P0 | E,P |
| get_tool_schema | OK | Кэш на 24h | P1 | E,P |
| get_categories | OK | readOnly, кэш | P2 | E |
| describe_tool | OK | То же | P2 | E |
| version_info | OK | readOnly | P2 | — |
| health_check | OK | timeout per backend, parallel | P1 | R,P |
| get_recent_errors | OK | pagination | P1 | E |
| reload_config | Race | per-session lock | P1 | R |

### 27. Google Sheets (46 tools) — **R0**
**Характеристика:** самая большая категория и самая частая. Лимиты Google (verbatim): «read request limit of 300 per minute per project»; per-user write — 60/min default; рекомендуемая payload ≤ 2 MB. batchUpdate первичен; atomicity внутри одного batch.
**Категорийные improvements:**
- batchUpdate всегда первичен. Если кто-то делает 3+ update_cell — wrap в один batch.
- Sheet ID vs name — резолвить через cache (name→id), а не каждый раз через get spreadsheet.
- `valueInputOption` всегда explicit (USER_ENTERED/RAW).
- `valueRenderOption` всегда explicit (FORMATTED_VALUE/UNFORMATTED_VALUE/FORMULA).
- `responseValueRenderOption` для read-after-write.
- ETag (spreadsheet `revisionId`) — не нативно, но можно через get spreadsheet metadata + sequence check.

Группировка по типам:

**Read (≈12 tools):** sheets_query, sheets_get_values, sheets_batch_get, sheets_get_metadata, sheets_get_named_ranges, sheets_get_sheet_id, sheets_get_chart, sheets_get_conditional_format, sheets_get_protected_ranges, sheets_list_sheets, sheets_get_properties, sheets_get_developer_metadata

| ALL read | Большой ответ | response_format; `range` mandatory; `valueRenderOption` explicit; кэш по spreadsheetId+range+revisionId 30s; readOnly=true, openWorld=true | P0 | E,P |

**Write cell-level (≈6):** sheets_update_cell, sheets_update_range, sheets_append, sheets_clear_range, sheets_clear_values, sheets_copy_paste

| sheets_update_cell | Дорогой если массово | Deprecation hint «use sheets_batch_update для >5 ячеек»; Idempotency; ETag-стиль через get revisionId before write | P0 | R,P |
| sheets_update_range | OK | Idempotency, dry_run, max_cells cap (например 10 000) | P0 | R,S |
| sheets_append | Дубликаты при retry | Idempotency-Key обязателен (Google sheets append НЕ идемпотентна нативно) | P0 | R |
| sheets_clear_range | DESTRUCTIVE | dry_run, undo log (сохранить значения до clear) | P0 | S |
| sheets_clear_values | То же | То же | P0 | S |
| sheets_copy_paste | OK | dry_run preview | P1 | R |

**Write batch (≈8):** sheets_batch_update, sheets_batch_clear, sheets_batch_update_by_data_filter, sheets_batch_get_by_data_filter и пр.

| sheets_batch_update | Главный workhorse | Idempotency-Key, requests-level dry_run (через `responseIncludeGridData=false`), per-request error envelope, max_requests cap 100 | P0 | R,S |
| остальные batch | Аналогично | То же | P0 | R |

**Sheet management (≈8):** sheets_add_sheet, sheets_delete_sheet, sheets_duplicate_sheet, sheets_copy_sheet_to, sheets_update_sheet_properties, sheets_move_sheet, sheets_set_active_sheet, sheets_rename_sheet

| sheets_delete_sheet | **Безвозвратно** | dry_run, undo (через duplicate перед удалением), require sheet has no protected ranges | P0 | S |
| sheets_add_sheet | Дубликаты по name | Idempotency, проверка conflict | P1 | R |
| sheets_duplicate_sheet | Дубликаты | Idempotency | P1 | R |
| остальные | OK | Idempotency, ETag | P1 | R |

**Formatting (≈6):** sheets_format_cells, sheets_set_borders, sheets_merge_cells, sheets_unmerge, sheets_freeze_rows_cols, sheets_auto_resize

| ALL formatting | OK, idempotent | annotations idempotent=true; всегда через batchUpdate | P1 | R,P |

**Conditional formatting (≈3):** sheets_add_conditional_format, sheets_delete_conditional_format, sheets_update_conditional_format

| ALL | OK | Idempotency, undo | P1 | R |

**Named ranges (≈3):** sheets_add_named_range, sheets_delete_named_range, sheets_update_named_range

| ALL | OK | Idempotency по name; undo | P1 | R |

**Charts (≈3):** sheets_add_chart, sheets_update_chart, sheets_delete_chart

| ALL | OK | dry_run для delete; idempotency | P1 | R |

**Misc:** sheets_protect_range, sheets_unprotect_range, sheets_set_data_validation, sheets_create_spreadsheet, sheets_filter_view_create/delete, sheets_set_basic_filter

| sheets_create_spreadsheet | Дубликаты | Idempotency-Key, явный parent_folder | P0 | R |
| sheets_protect_range | Влияет на чужой доступ | dry_run, listing affected users | P1 | S |
| остальные | OK | стандартные паттерны | P1 | R |

### 28. Google Slides (7 tools)
| slides_create | Дубликаты | Idempotency | P1 | R |
| slides_get | Большой | response_format | P1 | E,P |
| slides_batch_update | Race | batch первичен; idempotency на operations | P0 | R |
| slides_copy_presentation | OK | Idempotency | P1 | R |
| slides_replace_text | См. docs | dry_run, max_replacements | P0 | R,S |
| slides_get_thumbnail | Большое изображение | downsize, jpeg | P1 | P |
| slides_export | Большой | streaming | P1 | P |

### 29. Google Tasks (7 tools)
| tasks_list_tasks | OK | pagination, completed-filter | P1 | E,P |
| tasks_create | Дубликаты | Idempotency | P1 | R |
| tasks_update | OK | ETag, idempotency | P1 | R |
| tasks_complete | Idempotent by nature | annotations idempotent=true | P2 | — |
| tasks_delete | OK | undo (recreate) | P1 | S |
| tasks_list_tasklists | OK | readOnly | P2 | E |
| tasks_move | OK | dry_run, idempotency | P1 | R |

### 30. Translation (2 tools)
| translate | Argos может молча промазать на редком языке | Возвращать `confidence`; для критичных — second-pass | P1 | R |
| translate_probe | OK | readOnly | P2 | E |

### 31. Claim verification (1 tool)
| verify_claim | Сам по себе guardrail | Защитить от false-positive: возвращать structured verdict с подтверждающими цитатами; max_evidence cap | P0 | R,E |

### 32. Vision (2 tools)
| vision_ocr | Tesseract без preprocessing — низкая accuracy (на «трудных» сканах падает до 13–60%) | OpenCV pipeline: 300 DPI, grayscale → adaptive threshold → deskew → denoise; `psm` параметр (default 6 для plain text, 11 для sparse); `language` mandatory; confidence per word; `untrusted_content=true` | P0 | R,S |
| vision_probe | OK | readOnly | P2 | — |

### 33. Drive watcher (4 tools)
| watcher_poll_known_scripts | OK | rate-limited (1 раз в N минут); кэш | P1 | P |
| watcher_list_alerts | OK | pagination | P1 | E,P |
| watcher_mark_alerts_read | OK | Idempotency | P2 | R |
| watcher_recent_failures | Большой | response_format, since, severity-filter | P1 | E,P |

### 34. Wildberries (3 tools) — **R0**
**Категорийные improvements:**
- Token bucket headers (`X-Ratelimit-Limit`, `X-Ratelimit-Remaining`, `X-Ratelimit-Reset`, `X-Ratelimit-Retry`) парсить и уважать.
- `wb_finance_detail_collect` обращается к `https://statistics-api.wildberries.ru/api/v5/supplier/reportDetailByPeriod` — incremental по `lastChangeDate` cursor; verbatim лимит 80 000 строк per response; retry с курсором при 429.
- Token storage — keychain only; mask в logs; pre-flight check token category match endpoint scope; token валиден 180 дней.

| wb_check_token | OK | Validate JWT локально (decode без verify) для категорий перед сетью; verbatim лимит «A maximum of 3 requests every 30 seconds» соблюдать | P0 | R,P |
| wb_finance_detail_collect | До 80k строк; rate-limit; retry-сложность | Incremental cursor (`lastChangeDate`), idempotency на «collection_id», pagination, кэш per (token_hash, date_range, last_seen_rrdid), max_rows cap, `response_format`, progress callback или structured intermediate state | P0 | R,E,P |
| wb_token_age | OK | readOnly, кэш 1h | P2 | E,P |

### 35. Web fetch (2 tools)
| web_fetch | OWASP MCP — все три ноги «lethal trifecta» сходятся здесь | Mandatory: domain whitelist опц., max_size 5 MB, timeout 30s, `untrusted_content=true` marker; refuse if session already tainted by sensitive read; respect robots.txt opt-in | P0 | S |
| web_search | DuckDuckGo может уходить в null | retry, fallback, `response_format`, max_results cap | P1 | R,E |

---

## Сводная таблица топ-50 критичных улучшений (по всем категориям)

| # | Tool / scope | Improvement | Приор. |
|---|---|---|---|
| 1 | ALL 236 | MCP annotations (readOnly/destructive/idempotent/openWorld) | P0 |
| 2 | ALL destructive | Stripe-style Idempotency-Key infrastructure | P0 |
| 3 | ALL external API | Единый retry-декоратор с jitter + Retry-After | P0 |
| 4 | ALL | RFC 9457 problem+json error envelope | P0 |
| 5 | ALL responses | 25k tokens cap + truncation strategies | P0 |
| 6 | ALL destructive | Dry-run mode universal | P0 |
| 7 | ALL destructive | Undo log (sqlite, 30 days) | P0 |
| 8 | gmail_send / draft_send | Mandatory Message-ID idempotency + recipients confirm | P0 |
| 9 | drive_delete / empty_trash | trash-first + permanent require confirm | P0 |
| 10 | drive_share / permissions_add | dry_run + allowlist domains | P0 |
| 11 | sheets_batch_update | per-request error envelope + idempotency | P0 |
| 12 | sheets_append | idempotency обязательна (Google нативно не даёт) | P0 |
| 13 | sheets_clear_range/values | undo log + dry_run | P0 |
| 14 | sheets_delete_sheet | undo через duplicate-before-delete | P0 |
| 15 | apps_script_api_edit_file | snapshot → diff → version → write | P0 |
| 16 | apps_script_api_replace_function | AST-aware вместо regex | P0 |
| 17 | apps_script_api_run_function | Cloud project match pre-flight | P0 |
| 18 | apps_script staging | mandatory before edit | P0 |
| 19 | apps_script_get_bound_script_token | mask + audit + TTL | P0 |
| 20 | wb_finance_detail_collect | incremental cursor + token-bucket retry | P0 |
| 21 | wb_check_token | local JWT decode + 3 req/30s respect | P0 |
| 22 | gmail_search/list | response_format + partial fields | P0 |
| 23 | gmail_get | concise/detailed enum (cost 20 units!) | P0 |
| 24 | calendar_create_event | client-generated event ID idempotency | P0 |
| 25 | calendar_update_event | ETag/sequence concurrency | P0 |
| 26 | calendar_delete_event | undo + sendUpdates explicit | P0 |
| 27 | drive_list | pageSize cap + fields mandatory | P0 |
| 28 | drive_download | max_size + streaming | P0 |
| 29 | drive_move | dry_run + listing parents | P0 |
| 30 | contacts_update | ETag merge-on-conflict | P0 |
| 31 | contacts_delete | dry_run + 30-day retention | P0 |
| 32 | docs_replace_text | dry_run + match preview | P0 |
| 33 | slides_replace_text | то же | P0 |
| 34 | aliases_add | overwrite=false default | P0 |
| 35 | aliases_remove | last-use check + force flag | P0 |
| 36 | fs_write_file | atomic write + .bak | P0 |
| 37 | fs_delete | trash-first | P0 |
| 38 | bulk_payload_load | whitelist + size cap | P0 |
| 39 | browser_open | per-call incognito | P0 |
| 40 | browser_fill | mask secrets | P0 |
| 41 | browser_evaluate | restrict или запретить | P0 |
| 42 | vision_ocr | OpenCV preprocessing pipeline | P0 |
| 43 | web_fetch | trifecta enforcement + size cap | P0 |
| 44 | reply_lint | pre-hook на outbound tools | P0 |
| 45 | bank_parse_sber | confidence threshold + untrusted marker | P0 |
| 46 | chat_history_* | untrusted_content marker | P0 |
| 47 | list_tools | categorized output + cache | P0 |
| 48 | fx_rate | as_of + stale fallback | P0 |
| 49 | verify_claim | structured verdict + evidence cap | P0 |
| 50 | analytics_abc / abc_split | response_format + when-to-use | P1 |

---

## Roadmap внедрения (этапами)

### Этап 0 — Фундамент (2–3 недели)
- Shared infrastructure модули: `retry.py` (tenacity-style), `idempotency.py` (sqlite store), `errors.py` (RFC 9457 envelope), `truncation.py` (head/tail/summary), `cache.py` (LRU+TTL+sqlite), `audit.py` (JSON-lines), `undo.py` (sqlite WAL), `dry_run.py` (decorator pattern), `taint.py` (session marker).
- Helpers для MCP annotations: декоратор `@tool(read_only=True, idempotent=True, open_world=True, title="...")`.
- Pydantic strict-mode на всех input schemas; workaround для Claude Code issue #3084 (flat schemas или JSON-unwrap).

### Этап 1 — Надёжность (3–4 недели)
- Применить retry-декоратор ко всем external API tools (Sheets, Drive, Gmail, Calendar, Contacts, Apps Script, WB).
- Применить Idempotency-Key к 50 P0-destructive tools.
- Применить error envelope ко всем 236.
- Snapshot + version для всех apps_script edit-tools.

### Этап 2 — Эргономика (2–3 недели)
- response_format enum на 30 «толстых» tools.
- 25k tokens cap + truncation universal.
- list_tools categorized output.
- Docstrings refactor по Anthropic guidance (when-to-use, when-NOT-to-use, expected response shape, rate limits, response sizes).
- input_examples на complex schemas.

### Этап 3 — Производительность (2 недели)
- Cache layer на 40 read-only tools.
- Batch endpoints для Sheets/Drive/Contacts/Gmail.
- Incremental cursor для wb_finance, drive_list, gmail_list, list_executions.
- Partial responses + gzip для Google APIs.

### Этап 4 — Безопасность (2–3 недели)
- Dry-run mode на все 50 P0 destructive.
- Undo log + 30-day retention.
- Trash-first на delete-семейство.
- Taint tracking + lethal trifecta block.
- Mask секретов в logs/responses.
- Sandboxing browser_evaluate / fs_write / bulk_payload.

### Этап 5 — Полнота (1–2 недели)
- Гэп-аудит каждой категории: что отсутствует относительно полного покрытия Google/WB API.
- (Отдельный бэклог 100+ новых tools — out of scope этого аудита.)

---

## Cross-cutting concerns (shared infrastructure)

| Модуль | Назначение | Где используется |
|---|---|---|
| `retry.py` | Единый exponential backoff + jitter + Retry-After + classified retry (429/5xx vs 4xx) | Все external API tools |
| `idempotency.py` | Idempotency-Key store (sqlite, TTL 24h, scope=tool+user+args_hash), replay предыдущего ответа на повторный ключ | Все write tools |
| `errors.py` | RFC 9457 envelope, `retriable: bool, retry_after_ms, fix_hint, idempotency_key`, mapping HttpError→Problem | Все |
| `truncation.py` | head/tail/summary стратегии, token counter, `truncated: true, full_size: N` marker | Все list/get/search |
| `cache.py` | LRU + TTL + key=hash(args), sqlite-backed | Все read-only |
| `audit.py` | JSON-lines: ts, tool, args_hash, idempotency_key, status, duration, blast_radius | Все destructive |
| `undo.py` | sqlite WAL, last 100 ops per category, replay action | Все destructive |
| `dry_run.py` | decorator: `dry_run=True` → returns plan without execute | Все destructive |
| `taint.py` | session-state: tainted_after_tool, block exfiltration tools | Все openWorld + sensitive |
| `validators.py` | Pydantic strict mode, common patterns (range, email, date, drive_id, sheet_id) | Все |
| `mcp_annotations.py` | декоратор `@annotated_tool(read_only=, destructive=, idempotent=, open_world=, title=)` | Все 236 |
| `partial_response.py` | `fields` параметр для Google APIs (gzip + partial response) | Все Google read-tools |
| `quota_meter.py` | per-API в-минуту тарификация, proactive throttling | Sheets/Drive/Gmail/Apps Script/WB |

---

## Caveats / ограничения

1. **MCP protocol limitations.** Аннотации — только *хинты*, клиент может игнорировать. Реальную safety нельзя строить только на них; обязательны server-side enforcement. Сейчас Claude Code частично поддерживает аннотации (confirmation prompt для non-readOnly), но не все клиенты честно.
2. **Apps Script API не работает с service accounts** (verbatim из официальных docs) — все 27 tools работают исключительно через user OAuth. Это ограничивает автоматизацию в CI/CD без участия конкретного пользователя.
3. **Apps Script `updateContent` — atomic full replace** (verbatim: «This clears all the existing files in the project»). Нет PATCH. Любая ошибка в одном файле блокирует всё.
4. **WB API limit 80 000 строк в одном response** для финансовых endpoints (verbatim); incremental cursor по `lastChangeDate` — единственный путь.
5. **WB API token валиден 180 дней**, после чего silent revoke (статус 401 «token withdrawn»). OAuth-токены WB обновляются автоматически, regular — нет; нужен мониторинг.
6. **Claude Code как клиент** не имеет direct Claude API доступа — это значит, что любая «smart» постобработка (summarization, ranking) должна происходить либо на стороне MCP-сервера локально (Argos translate, regex-based summarizer), либо никак. Recommendation chain агента — сам Claude через инструменты.
7. **FastMCP namespace bug** (issue #2596) — префиксы при composition могут не урезаться до 56 char. Делать namespace вручную, не полагаться на `mount(prefix=)`.
8. **Pydantic strict mode + Claude Code конфликт** (issue anthropics/claude-code#3084): Claude Code сериализует nested objects как JSON-строки. Workaround: использовать flat schemas или явную JSON-string обёртку через декоратор.
9. **Google Workspace quota changes May 2026**: для проектов, созданных после 1 мая 2026, действуют новые квоты (например, Gmail 1 200 000 units/min/project + 6 000 units/min/user/project) с потенциальным billing после ноября 2026. Закладывать proactive quota meter.
10. **OWASP MCP Top 10 не enforce** через спецификацию — это организационные требования v0.1. Top-3 risk classes сейчас: MCP01 «Token Mismanagement & Secret Exposure» (это `auth_*` и `apps_script_get_bound_script_token`), MCP06 «Prompt Injection via Contextual Payloads» (это все `untrusted_content` tools), MCP07 «Insufficient Authentication & Authorization». Prompt Injection — НЕ #1 в MCP-специфическом списке (в общем OWASP LLM Top 10 это LLM01:2025, но в MCP отдельный приоритет).
11. **Undo log не покрывает Gmail permanent delete и Drive permanent delete** — Google не даёт нативного восстановления для permanent. Единственная защита — никогда не делать permanent без двойного confirm.
12. **Headless Playwright на серверах** требует residential proxy для anti-bot — это отдельная инфраструктура, не покрывается этим бэклогом.
13. **Tesseract accuracy на «трудных» сканах**: даже с OpenCV preprocessing — типично 60–95% character accuracy. Для критичных финансовых OCR (sber выписки) — обязательна human verification или второй проход PaddleOCR/EasyOCR с консенсусом.
14. **«25 000 tokens» как hard limit** — это рабочий внутренний дефолт, основанный на широкой практике комьюнити MCP. В первичной статье Anthropic «Writing effective tools for agents» (Sep 11, 2025) конкретная цифра не вынесена нормативно; рекомендация формулируется качественно («restrict responses», «sensible defaults», «pagination, filtering, truncation»). Закрепляем 25k как стартовый порог с возможностью тюнинга под конкретный tool.
15. **Apps Script API quotas (RPM/RPD)** для самого `script.googleapis.com` Google официально не публикует на developers.google.com; они видны и редактируются только в Cloud Console квот конкретного проекта. Использовать project-level monitoring через Cloud Console.

Этот документ — рабочий бэклог. Перед началом этапа 0 рекомендуется превратить общие принципы в shared library (`workspace_agent_core/`) и пилотировать на 5 P0-tools (`drive_delete`, `sheets_batch_update`, `gmail_send`, `apps_script_api_edit_file`, `wb_finance_detail_collect`) до раскатки на весь набор.