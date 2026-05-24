# Session handoff — 2026-05-24

## TL;DR

29 commits landed on `origin/main` of `github.com/nnenoix/Assistant`
(formerly `nnenoix/Google-work`). Repo is **public**, first
release `v0.1.0` is built + downloadable. The agent gained: chat
history with sidebar UI, knowledge-base tools (semantic recall
across chats), browser-rendered web fetch for anti-bot pages, native
folder picker, auto-update channel, end-to-end OAuth multi-account
resolution. **2251 unit tests passing.**

Open the file in a fresh session and the new agent should be able to
continue without re-reading the whole 200k-token thread.

---

## What the user is doing

Building a personal AI agent for working with Google Workspace +
Russian e-commerce (WB / Ozon / МойСклад / etc.). Distributed as a
local `.exe` via PyInstaller, eventually for friends / small team.

- **Primary chat:** `2026-05-24T02-18-29` ("Открой папку https://drive
  .google.com/drive/folders/1BP6m-gcgAo2EY3V1JSo_2d1jtgvYEZ1-")
- That folder = "Аналитика тру статс" (Elena's drive). 5 files
  including TrueStats_Еженедельный_отчёт_шаблон_1.docx.
- Last live exercise: agent fetched + saved 9 .txt files about
  WB/Ozon "продвижение товаров" docs into `D:/tmp/prodvizhenie/`
  AND (via backfill script) into the semantic knowledge base.

## Working setup

```powershell
cd "D:\Google work"
uv run uvicorn src.app:app --host 127.0.0.1 --port 8765
# Open http://127.0.0.1:8765
```

Available OAuth accounts: `egor.titt`, `elena`,
`elenatitarenko247.backup`, `main` (4 total).

Stop server:
```bash
netstat -ano | grep :8765 | head -1 | awk '{print $5}' \
  | xargs -I{} powershell -c "Stop-Process -Id {} -Force"
```

Send a prompt without dealing with cp1251 encoding:
```python
uv run python -c "
import json, urllib.request
msg = 'твой промт'
data = json.dumps({'message': msg, 'chat_id': '2026-05-24T02-18-29'}).encode('utf-8')
req = urllib.request.Request('http://127.0.0.1:8765/chat', data=data,
                              headers={'Content-Type':'application/json'}, method='POST')
print(urllib.request.urlopen(req).read().decode())
"
# Then read SSE stream by run_id, write to .data/run_trace.txt
# (cp1251 in shell breaks bare print — always go through io.open(file, encoding='utf-8'))
```

---

## What got built this session (29 commits)

In commit order (oldest first):

| SHA | Topic | Why |
|---|---|---|
| `4b7e5f3` | chore: drop dead `_paginate.py` + ignore Claude lockfile | Housekeeping |
| `8f8ee0a` | MCP HTTP / Telegram / vendor cleanup + 7 sec findings | Code-review + security pass |
| `5bca513` | Path-traversal guards (MDM, bulk, vendor caches) | Sec C2 + sweep |
| `b2a8cf7` | Per-tenant idempotency cache (tenant_id PK) | SEC M1 |
| `c596e4e` | `/metrics` endpoint (zero-dep Prometheus text emission) | Observability |
| `f3083b3` | OTel span attributes + record_exception | Observability |
| `ccf79b6` | `scripts/migrate_jsonl_to_pg.py` | Phase 0 followup |
| `0b31316` | Shared `_vendor_http` for edo+social | Refactor |
| `94531e2` | LibreChat OIDC config + Kubernetes manifests | Phase 0 followup |
| `5d229bc` | `src/updater.py` — version checker | App auto-update Part 1 |
| `3add752` | TrueStats `.docx` template renderer + 2 tools | User-asked feature |
| `e642115` | Code-review fixes (8 items, see commit body) | Quality pass |
| `66464c2` | Wall-clock regression for verify_claim parallelism | Phase 14D test |
| `68beef1` | Bulk INSERT via executemany in migrate | Perf |
| `1803876` | Cleanup sweepers (idempotency expire + audit rotate) + WAL pragma | Bounded growth |
| `0a0324e` | `updater.download_update` + `apply_update` (Windows-aware self-replace) | Auto-update Part 2 |
| `74b3c2b` | Telegram bot sidecar in docker-compose + GitHub Actions CI | Ops |
| `daebf8f` | Route all 10 vendor modules through `_vendor_http.request_raw` | Refactor |
| `00d9252` | `drive_browser_open` / `drive_browser_list_folder` — Playwright Drive UI fallback | Drive multi-account |
| `e51a2c8` | `drive_resolve_link` + system prompt rule 23a + onboarding endpoint + `/api/updates/*` | Multi-account auto-resolve |
| `29ab10b` | `.github/workflows/release.yml` + `UpdateBanner` UI + `docs/DISTRIBUTION.md` | Distribution machinery |
| `8c2a3fe` | Wire `UPDATE_MANIFEST_URL` to real GitHub URL | First real release |
| `6b2a988` | Update URL after repo rename Google-work → Assistant | Cosmetic |
| `3717a12` | README rewrite for public-facing audience | Repo went public |
| `ad38f95` | `drive_resolve_link` probes accounts in PARALLEL (was serial — 4×150ms → 200ms) | User feedback "долго" |
| `cab9d49` | Persistent chat history + sidebar UI + per-chat resume via recap | User feedback "нет памяти между чатами" |
| `59e7ead` | `browser_fetch_text` (Playwright) + native folder picker + VersionInfo for .exe | User asked for all 3 |
| `7c00f8d` | `knowledge_save/search/list_sources/delete` — local semantic KB | "сохранить локально для анализа" |
| `66dc758` | System prompt rule 23b (use knowledge_save) + 23c (stop URL-guessing) | Live observation — Sonnet ignored knowledge_save, went into 30-turn URL guessing loop |

## Test counts at finish

- **2251 passing**, 23 skipped, 1 deselected (`test_missing_file_creates_empty_policy` —
  pre-existing red, see CLAUDE.md).
- **+286 vs session start** (was 1965).

## Key new modules / files

| File | Purpose |
|---|---|
| `src/tools/knowledge.py` | save/search/list_sources/delete over `embeddings.py` |
| `src/tools/_vendor_http.py` | Shared GET/POST + `request_raw` for 10 vendor modules |
| `src/tools/_safe_id.py` | Shared `^[A-Za-z0-9_-]{1,64}$` validator |
| `src/metrics.py` | Zero-dep Prometheus text emission + `/metrics` mount |
| `src/updater.py` | check / download / apply_update (Windows-aware swap) |
| `src/telegram_bot_loop.py` | Daemon entry point for docker-compose telegram sidecar |
| `src/tools/_docx_template.py` | Fill .docx templates by `{placeholder}` syntax |
| `scripts/migrate_jsonl_to_pg.py` | `.data/infra/*.jsonl` → Postgres bulk-INSERT |
| `.github/workflows/test.yml` | CI pytest on push/PR |
| `.github/workflows/release.yml` | Build .exe + manifest on `v*` tags |
| `deploy/k8s/` | 6 YAML manifests (namespace/configmap/secret/postgres/redis/agent/ingress) |
| `version_info.txt` | Windows EXE VersionInfo (Firewall popup name) |
| `config/librechat-oidc.example.yaml` | LibreChat ↔ Authentik SSO config |
| `docs/DISTRIBUTION.md` | How to ship .exe to friends end-to-end |
| `tests/test_chat_history.py` | 23 tests for chat persistence + endpoints |
| `tests/test_knowledge.py` | 18 tests for KB tools |
| `tests/test_browser_drive_open.py` | 25 tests for Drive UI fallback |
| `tests/test_drive_resolve_link.py` | 12 tests incl. parallel-timing regression |
| `tests/test_telegram_bot_security.py` | 13 tests for auth gate / allowlist / id guards |
| `tests/test_vendor_helpers_security.py` | 6 tests for OAuth cache path-traversal |
| `tests/test_metrics.py` | 15 tests for /metrics endpoint |
| `tests/test_updater.py` | 45 tests for version check + download + apply |
| `tests/test_otel_instrumentation.py` | 8 tests for span attribute population |
| `tests/test_migrate_jsonl_to_pg.py` | 24 tests for migrator (incl. bulk-insert regression) |

## Distribution state

- **Repo:** https://github.com/nnenoix/Assistant — **public**, MIT
- **First release:** https://github.com/nnenoix/Assistant/releases/tag/v0.1.0
  - `workspace_agent.exe` (25.3 MB) + `manifest.json` with SHA-256
- **`UPDATE_MANIFEST_URL`** baked into `src/config.py` →
  `releases/latest/download/manifest.json` (works without auth)
- **client_secret*.json** already in repo root, gitignored — bundled
  into .exe by PyInstaller spec
- **CI:** GitHub Actions runs pytest on push/PR. On `v*` tags also
  builds .exe + uploads to Release

## Knowledge base contents (live state)

`embeddings.sqlite` scope `knowledge` has 9 entries from a backfill
script run today — WB/Ozon "продвижение товаров" pages:

```
wb_1_modeli_oplaty                  source: seller.wildberries.ru/.../wb-promotion-payment-models-and-working-principles
wb_2_zapusk_kampanii                source: seller.wildberries.ru/.../how-to-start-and-edit-promotion-campaigns
wb_3_statistika                     source: seller.wildberries.ru/.../statistics-and-promotion-management
wb_4_stavki                         source: seller.wildberries.ru/.../how-to-choose-bid-for-promotion
ozon_instrumenty_prodvizheniya      source: docs.ozon.ru/.../product-promotion
ozon_cpc_4_nedelnyi_byudzhet        source: docs.ozon.ru/.../pay-per-click/weekly-budget
ozon_cpo_2_zapustit_prodvizhenie    source: docs.ozon.ru/.../pay-per-order/launch-promotion
ozon_cpo_4_rezultaty                source: docs.ozon.ru/.../pay-per-order/view-results
ozon_cpo_5_morkovsk                 source: docs.ozon.ru/.../pay-per-order/morkovsk
```

Verified semantic search works:
- "минимальная ставка Ozon" → ozon_instrumenty_prodvizheniya (0.598)
- "WB бюджет кампании" → ozon_cpc_4_nedelnyi_byudzhet (0.585)

Same 9 files also live at `D:/tmp/prodvizhenie/*.txt` (the agent's
own save path before rule 23b was added).

## Known issues / followups

### Behavior to verify next session
- Rule 23b in `src/agent.py` lines 233-239 should make Sonnet pick
  `knowledge_save` over `local_write_file` for "save for analysis"
  prompts. NOT YET VALIDATED in a real run.
- Rule 23c should stop URL-brute-forcing after 3 404s. NOT YET VALIDATED.

### Stray artefacts
- `x` file in working tree — 2-byte CRLF leftover from earlier sessions.
  Unrelated to my changes. Leave alone or `rm x` if user wants clean tree.
- `D:/tmp/prodvizhenie/` — 9 .txt files duplicating what's now in KB.
  Safe to delete; KB has them indexed.

### Tools that could improve precision (described but NOT built)
- `sheets_get_schema(id)` + schema cache so agent doesn't re-discover
  table structure each turn
- TTL-cache on `web_fetch` / `drive_get_metadata` (separate from
  idempotency cache which is write-side)
- `reply_check` enforcement: web-facts must carry `[source:url]`
- `task_checkpoint(label)` / `task_resume(label)` for long
  multi-step tasks
- `agent_say_dont_know(why)` explicit tool — better than hallucinating
- `web_search_url(domain, hint)` to bypass the URL-guessing pattern

### Distribution polish
- OAuth verification (required for >100 users, ~1-4 weeks process)
- Frontend "what's new in this version" pane after auto-update
- Sentry / error-reporting webhook so we see crashes from .exe users

### Bigger refactors that didn't happen (deliberately)
- `_wrap_for_sdk` middleware-chain refactor (declined 3× as
  Simplicity-First violation)
- `error_kind: Literal[...]` types (touches 50+ files)
- Vendor `_request` consolidation past 10/10 (current state is "all
  route through `_vendor_http.request_raw` but each module keeps its
  auth-shape adapter")

## Quick "what state is everything in" check

```bash
cd "D:/Google work"
git log --oneline -3                    # 66dc758 is HEAD
git status --short                       # only `?? x` should appear
uv run pytest tests/ -q \
  --ignore=tests/integration \
  --deselect "tests/test_policy.py::test_missing_file_creates_empty_policy"
# expect: 2251 passed, 23 skipped, 1 deselected
curl -sL https://github.com/nnenoix/Assistant/releases/latest/download/manifest.json
# expect: JSON with latest_version=0.1.0, download_url, sha256
```

## Suggested first action next session

Either:

1. **Validate rule 23b in the wild.** Send the agent another
   "прочитай и сохрани локально" prompt and check the trace —
   it should now call `knowledge_save`, not `local_write_file`.

2. **Build one of the precision tools** from the list above. The
   cheapest + highest-leverage is probably `task_checkpoint` /
   `task_resume` so long multi-step workflows survive a SDK
   session reset.

3. **OAuth verification** — start the process so the "App not
   verified" warning eventually goes away for new users.

4. **Frontend polish for chat sidebar** — auto-titling via Claude
   (4-word title from first exchange) + chat search box in
   sidebar (`knowledge_search` style).

User's typical mode: short directive prompts ("делай", "теперь
public", "так далее"). Don't over-explain. Pick the path, do it,
report concisely. They will redirect if it's wrong.
