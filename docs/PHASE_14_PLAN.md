# План: Phase 14 — production scale (100 файлов × 7M символов)

## Контекст

Текущий стресс-тест прошёл 250k строк и 200 файлов — но это **на порядок меньше реальной production-нагрузки**. Реальный сценарий пользователя:

- **4 продавца × 5 магазинов = 20 точек**
- **~5 финансовых книг на точку × 20 = 100 spreadsheets**
- **~7M символов в каждой** (ОПиУ + ДДС + Баланс + Year_fact + Year_plan + Месяцы факт/план + bound Apps Scripts)
- **Всего ~700M символов данных** + формулы + связи между книгами

Аудит показал что архитектура к этой нагрузке **не готова**:

1. **Нет cross-spreadsheet tools.** `sheets_batch_read` берёт один `spreadsheet_id`. `sheets_query` тоже. Чтобы прочитать одну метрику из 100 книг — 100 последовательных вызовов. На 800ms/запрос = **80 секунд серийно**.
2. **Нет внутренней параллелизации.** Все вызовы tool'ов идут через `asyncio.to_thread` но **никогда** `asyncio.gather`. `verify_claim` с 50 refs из stress test показал p50 **47 секунд** — это ровно из-за serial reads.
3. **`MAX_TOOL_PAYLOAD = 12 000 байт`.** Если попытаться вернуть 100-файловый агрегат — `_wrap_for_sdk` тихо обрежет на 11.6KB. Полезная инфа теряется.
4. **Нет session-level кеша.** Один и тот же `(spreadsheet_id, range)` агент может перечитывать 5 раз за turn — каждый раз свежий API-вызов.
5. **Нет защиты от Google quota.** RetryingHttpRequest спасает от 429 пост-фактум (5 ретраев, ~62s окно), но если в **одной секунде** агент стрельнёт 100 reads — это `60/min` limit × 2 + ретраи на бэкоффе = резкий деградейт.
6. **`apps_script_oneshot` — единственный «escape hatch»**, но он verbose и не очевидно когда использовать.

**Цель Phase 14:** сделать так чтобы **«найди прибыль по всем 20 магазинам»** выполнялось за ~5 секунд вместо 80, **«суммируй по всем 100 книгам»** — за 10 секунд, и **агент сам выбирал bulk-инструмент при сценариях с 5+ файлами**.

## Принципы

1. **Asyncio.gather везде где можно.** Tool wrapper остаётся sync (registry упаковывает через `asyncio.to_thread`), но **внутри bulk-tools** мы используем `httpx` или `concurrent.futures.ThreadPoolExecutor` для параллельных API-вызовов.
2. **Apps Script для тяжёлых агрегаций.** При 50+ файлов и однотипной операции — генерируем Apps Script одним вызовом. Один round-trip, server делает всю работу.
3. **Payload-aware design.** Bulk-tools возвращают **компактные** агрегаты (per-file: id + value + 2-3 поля), не полные `values` массивы. 100 файлов × 80 байт = 8KB ≤ MAX_TOOL_PAYLOAD.
4. **Per-file errors не валят batch.** Один сломанный spreadsheet в 100 не должен убивать остальные 99.
5. **`_meta` envelope сохраняется.** Bulk-результаты возвращают `_meta.per_file_status` (счёт ok/error) — агент видит сколько успешно.
6. **Cache — opt-in.** TTL-кеш по умолчанию выключен (риск стейл-чтений после write); включается per-session.
7. **Quota budgeter — always on**, но логирует только когда реально пейсит.

---

## 14A. `sheets_bulk_metric(spreadsheet_ids, metric, period?)`

**Цель:** один вызов = метрика из N книг.

```python
def bulk_metric(
    spreadsheet_ids: list[str],
    metric: str,
    period: str | None = None,
    max_concurrent: int = 10,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Run `metric_lookup` across N spreadsheets in parallel.

    Returns {results: [{spreadsheet_id, ok, value?, cell?, row_label?,
    col_label?, strategy?, error?}], _meta:{ok_count, error_count,
    duration_ms}}.
    """
```

**Реализация:** `concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrent)`, каждый поток вызывает `metric_lookup(sid, metric, period, account)`. Исключения ловятся per-spreadsheet и кладутся в `result.error`.

**Файл:** [src/tools/sheets.py](src/tools/sheets.py) — после `metric_lookup`.

**Registry:** `sheets_bulk_metric` с описанием «for N≥5 spreadsheets — use this instead of looping metric_lookup».

---

## 14B. `sheets_bulk_read(refs, max_concurrent=10)`

**Цель:** generic параллельный `read_range` для произвольных `{spreadsheet_id, range}` пар.

```python
def bulk_read(
    refs: list[dict],  # [{"spreadsheet_id":..., "range":..., "formatted":bool?}, ...]
    max_concurrent: int = 10,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Parallel read_range across refs."""
```

Каждый ref может указать свой spreadsheet/range. Возврат: `[{ref, ok, values?, range_read?, error?}]`. Payload компактится при >50 ref'ах: только первая ячейка + dimensions.

---

## 14C. `sheets_cross_aggregate(spreadsheet_ids, sheet, cell, op)`

**Цель:** server-side агрегация через автогенерируемый Apps Script. Один вызов → server открывает все N книг → возврат скаляра или списка.

```python
def cross_aggregate(
    spreadsheet_ids: list[str],
    sheet: str,           # tab name in each book
    cell: str,            # cell ref, e.g. "B45"
    op: str = "sum",      # "sum"|"avg"|"min"|"max"|"count"|"list"
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Server-side cross-book aggregation. Generates Apps Script that opens
    each spreadsheet, reads `sheet!cell`, applies op. One round-trip."""
```

**Реализация:** под капотом строит JS:
```javascript
function main() {
  var ids = [...];
  var vals = [];
  for (var i = 0; i < ids.length; i++) {
    try { vals.push(SpreadsheetApp.openById(ids[i]).getRange("<sheet>!<cell>").getValue()); }
    catch (e) { vals.push(null); }
  }
  // apply op on vals
  return { result: ..., per_file: vals };
}
```

И вызывает `apps_script_oneshot(code, keep_project=False)`.

Returns `{value, per_file_count, errors_count, _meta:{op, sheet, cell}}` для скаляров, либо `{values: [...], _meta}` для `op="list"`.

**Файл:** [src/tools/sheets.py](src/tools/sheets.py) + reuse [src/tools/macros.py:apps_script_oneshot](src/tools/macros.py).

---

## 14D. Параллелизация `verify_claim`

В [src/tools/verify.py:verify_claim](src/tools/verify.py): сейчас цикл `for raw_ref in source_refs: result = _verify_one(ref)` — serial. Stress-test показал **47s p50** для 50 refs.

**Фикс:** заменить цикл на `ThreadPoolExecutor` с `max_workers=10`. Per-ref errors стандартно ловятся.

Ожидаемый эффект: 47s → ~5-7s (определяется самым медленным ref'ом).

**Файл:** [src/tools/verify.py](src/tools/verify.py).

---

## 14E. Session-level TTL cache для sheet reads

**Цель:** в рамках turn'а / короткой сессии не перечитывать те же ячейки.

```python
# src/tools/_read_cache.py (new)
class SheetReadCache:
    """LRU + TTL cache for sheet read results.

    Keyed by (account, spreadsheet_id, range, formatted).
    Default TTL = 300s. max_entries = 500.

    Disabled by default — enable via env SHEETS_READ_CACHE=1.
    """
```

`sheets.read_range` / `sheets.read_named_range` / `sheets.batch_read` checkают кеш ДО API-вызова, кладут результат ПОСЛЕ. `_meta.from_cache=True` когда был hit.

**Опциональность:** значение по умолчанию off, потому что для агента который пишет/перечитывает один и тот же диапазон в одном turn'е stale-cache может быть невидимой проблемой. Но для read-heavy сценариев включается.

**Файл:** `src/tools/_read_cache.py` (new). Обёртка в `sheets.read_range/etc`.

---

## 14F. QuotaBudgeter

**Цель:** проактивно пейсить вызовы когда приближаемся к 60/min на пользователя.

```python
# src/tools/_quota.py (new)
class QuotaBudgeter:
    """Sliding-window rate limiter.

    Configured for Google Sheets (60 reads/min/user). When 50 reads logged
    in the last 55s, the next `acquire()` sleeps to space out.

    Per-service: sheets, gmail, drive separate buckets.
    """
```

Интегрируется в `_service()` или `_wrap_for_sdk`. Логирует в `_meta.quota_paced_ms` когда применил.

**Файл:** `src/tools/_quota.py` (new).

---

## 14G. System prompt rules + tool descriptions

Новые правила в [src/agent.py](src/agent.py) после правила 25:

> **26. Bulk-first для N≥5 файлов.** Если задача затрагивает ≥5 spreadsheets с одной операцией (метрика, чтение, агрегат) — используй `sheets_bulk_metric` / `sheets_bulk_read` / `sheets_cross_aggregate`. **Никогда** не итерируй `sheets_read_range` или `sheets_metric_lookup` по 20 файлам вручную — это серийная задержка + риск исчерпать quota.
>
> **27. Apps Script для 50+ файлов.** Когда нужна агрегация / сложная логика поверх 50+ книг — `sheets_cross_aggregate` (для простых ops) или `apps_script_oneshot` (для произвольной логики). Server-side обработка устраняет 100× API round-trip cost.
>
> **28. Параллельная верификация.** `verify_claim` теперь параллельный — спокойно передавай 50+ refs за раз.

Описания новых tools должны явно рекомендовать использовать их для bulk-кейсов.

---

## 14H. Production-scale stress test

**Файл:** `scripts/stress_production.py` (new).

**Сценарий:**
1. **Build phase** (~15 минут):
   - Создать `CLAUDE-TEST/production/<timestamp>/` папку
   - 20 spreadsheets (масштабированный сценарий вместо 100 — иначе 1 hour превращается в 5)
   - Каждая книга содержит:
     - 4 sheets: «Год факт», «Год план», «Месяцы», «История» 
     - ~70k cells per book = ~700k chars per book (масштабированно, реальный production 7M = ×10)
     - Realistic P&L structure: 50 метрик × 13 периодов в Год факт
     - Year/plan columns с формулами `=SUM(...)`, `=AVERAGE(...)`
   - Имена с realistic кириллицей: «Продавец1_Магазин3_ОПиУ_2026.xlsx», и т.д.

2. **Exercise phase** (~30 минут):

   | Test | Сценарий | Expected outcome |
   |---|---|---|
   | T1 | `sheets_bulk_metric(20_ids, "Чистая прибыль", "Год факт")` | <10s, 20 valid results |
   | T2 | Compare: 20× serial `metric_lookup` (control) | ~80s — confirms 8-10x win |
   | T3 | `sheets_cross_aggregate(20_ids, "Год факт", "Чистая прибыль", op="sum")` | <15s, total revenue across 20 stores |
   | T4 | `sheets_bulk_read` 100 refs across 20 books (5 ranges each) | <20s |
   | T5 | `verify_claim` with 100 refs across 20 books | <15s p50 (vs current 50× ~47s) |
   | T6 | Enable cache, repeat T1 | <2s (cached) |
   | T7 | Rapid-fire 100 reads in 30s — quota budgeter pacing | `_meta.quota_paced_ms` > 0 |
   | T8 | Mixed workflow: drive_search → bulk_metric → cross_aggregate → pdf_create | end-to-end <60s |

3. **Report phase**:
   - `summary.json` с per-stage timing + per-file status
   - `progress.log` realtime
   - Comparison table: serial vs bulk for each operation

**Acceptance:** все 8 тестов проходят за < 60 минут wall-clock total.

---

## Файлы

**Новые:**
- `src/tools/_read_cache.py` — SheetReadCache (TTL+LRU)
- `src/tools/_quota.py` — QuotaBudgeter (sliding window)
- `scripts/stress_production.py` — production-scale builder + exerciser
- `tests/test_bulk_tools.py` — unit tests для bulk_metric/bulk_read/cross_aggregate
- `tests/test_read_cache.py` — TTL/LRU eviction
- `tests/test_quota_budgeter.py` — sliding window pacing

**Изменения:**
- [src/tools/sheets.py](src/tools/sheets.py) — `bulk_metric`, `bulk_read`, `cross_aggregate`; опциональная интеграция кеша в `read_range`/`read_named_range`/`batch_read`
- [src/tools/verify.py](src/tools/verify.py) — параллелизация `verify_claim` через ThreadPoolExecutor
- [src/tools/registry.py](src/tools/registry.py) — регистрация 3 новых tools, обновление описаний; optionally — quota budgeter integration в `_wrap_for_sdk`
- [src/agent.py](src/agent.py) — правила 26-28

**Net tools:** 226 → 229 (+3 bulk).

## Что переиспользуем

- [`apps_script_oneshot`](src/tools/macros.py) — основа `cross_aggregate`, не пишем заново
- [`sheets.metric_lookup`](src/tools/sheets.py) — основа `bulk_metric`, не пишем заново
- [`sheets.read_range`](src/tools/sheets.py) — основа `bulk_read`, не пишем заново
- `ThreadPoolExecutor` — standard library, не нужны новые deps
- [`_classify_exception`](src/tools/registry.py) — для per-file error categorization в bulk results
- [`_wrap_for_sdk`](src/tools/registry.py) — без изменений, новые tools регистрируются стандартно

## Что НЕ делаем

- Не пишем настоящих 7M chars × 100 books в тесте — занимает 5+ часов чистого creation time. Тестируем на 20 × 700k = масштабированный production с теми же patterns.
- Не делаем cache by default. Read-heavy сценарии включают через env / per-session toggle.
- Не пишем свой rate limiter с нуля если можно использовать `RetryingHttpRequest` + budgeter (последний — proactive, первый — reactive).
- Не трогаем MAX_TOOL_PAYLOAD = 12000. Bulk-tools должны быть payload-aware by design.

---

## Верификация

1. **Unit-уровень:**
   - `pytest tests/test_bulk_tools.py` — bulk_metric/bulk_read/cross_aggregate с мок-сервисом
   - `pytest tests/test_read_cache.py` — TTL expiration, LRU eviction, cache miss/hit
   - `pytest tests/test_quota_budgeter.py` — sliding window, pacing trigger
   - `pytest tests/test_verify.py` (extend) — параллельный verify_claim корректно собирает результаты, ошибка одного ref не валит остальные

2. **Integration (CLAUDE-TEST):**
   - `pytest tests/integration/test_phase14_bulk_live.py` — 5 кейсов на CLAUDE-TEST/phase-14/:
     - bulk_metric на 5 mock-spreadsheets
     - cross_aggregate sum на 5 mock-spreadsheets
     - bulk_read 20 refs across 5 books
     - cache hit на повторе bulk_metric
     - verify_claim 20 refs параллельно

3. **Production stress (~60 min):**
   - `LIVE_GOOGLE_TESTS=1 uv run python scripts/stress_production.py`
   - 20 книг × ~700k символов
   - 8 acceptance тестов из секции 14H
   - Сравнительная таблица: bulk vs serial для каждого

4. **Manual UI smoke:**
   - «найди чистую прибыль по всем магазинам Продавца1» — агент должен использовать `sheets_bulk_metric` или `cross_aggregate` (видно в tool_call), не итерировать 5 metric_lookup
   - «суммируй прибыль всех точек» — должен выбрать `cross_aggregate`

## Готовность к мержу

- Все 300+ существующих unit-тестов остаются зелёными
- Новые тесты (~25) зелёные
- Integration phase-14 зелёный (LIVE_GOOGLE_TESTS=1)
- Production stress: 8/8 acceptance pass + comparison table показывает 5-10× speedup на bulk vs serial
- System prompt rules 26-28 на месте
- `git tag phase-14`
