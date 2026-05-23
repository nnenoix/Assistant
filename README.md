# Workspace Agent

[![tests](https://github.com/nnenoix/Assistant/actions/workflows/test.yml/badge.svg)](https://github.com/nnenoix/Assistant/actions/workflows/test.yml)
[![release](https://github.com/nnenoix/Assistant/actions/workflows/release.yml/badge.svg)](https://github.com/nnenoix/Assistant/actions/workflows/release.yml)

Локальный AI-агент, который реально делает работу в Google Workspace и
российском e-commerce-стеке. Чат принимает естественный язык, агент
вызывает нужные инструменты, ты видишь результат и подтверждаешь
любые изменения.

**Без API-ключей.** Использует подписку Claude Pro/Max через `claude` CLI.

```
"Сделай выгрузку остатков WB и Ozon за май, положи в таблицу X,
 проверь что суммы совпадают с МойСклад, отправь отчёт в Telegram."
```

Один запрос — несколько API, проверки между источниками, подтверждение
перед записью.

## Что внутри

### Google Workspace
- **Drive / Sheets / Docs / Slides / Forms / Tasks / Gmail / Calendar / Contacts** — чтение, поиск, запись с подтверждением, bulk-операции на сотни листов параллельно.
- **Apps Script** — clone / push / run, обход хитрых API-лимитов через Playwright (поиск bound script ID, привязка к GCP-проекту).
- **Multi-account** — несколько Google-аккаунтов одновременно, агент сам выбирает под каким работать через `drive_resolve_link`.

### Российский e-commerce и финансы
- **Маркетплейсы**: Wildberries, Ozon, ЯндексМаркет
- **Учёт**: МойСклад, 1С OData
- **Платежи**: ЮKassa, Тинькофф Acquiring
- **ЭДО**: СБИС, Контур.Диадок
- **Логистика**: СДЭК, Boxberry, Почта России
- **Соцсети**: Avito, VK
- **Мессенджеры**: SMS.ru, SMSC.ru, Telegram Bot
- **Данные**: DaData (адреса, реквизиты по ИНН), Natasha NER

### Инфраструктура
- **2200+ unit-тестов** в CI
- **405+ инструментов** в едином registry
- **Multi-tenant scaffolding** (OIDC через Authentik, RBAC через Casbin, Postgres миграции, arq queue, Redis, OTel + Prometheus метрики)
- **MCP HTTP-transport** — можно подключить как сервер к LibreChat, Claude Desktop, Open WebUI
- **Auto-update** — встроенный механизм проверки + установки новых билдов
- **Kubernetes manifests** + Docker Compose для self-host

## Быстрый старт

### Для разработчиков — запуск из исходников

```powershell
# Windows
git clone https://github.com/nnenoix/Assistant.git
cd Assistant
winget install --id=astral-sh.uv -e
uv sync
uv run uvicorn src.app:app --host 127.0.0.1 --port 8765
# Открой http://127.0.0.1:8765
```

При первом запуске откроется wizard:
1. Установить Claude Code CLI (одной кнопкой).
2. Войти в Anthropic (Pro/Max подписка).
3. Войти в Google.

После — открывается чат, можешь работать.

### Для конечных пользователей — готовый .exe

Скачать последнюю сборку: **[Releases](https://github.com/nnenoix/Assistant/releases/latest)**

`.exe` сам пройдёт через wizard и подхватывает обновления автоматически.

## Архитектура

```
┌─────────────┐  POST /chat
│  pywebview  │ ──────────► FastAPI ─► AgentSession ─► Claude CLI
│  UI         │             (src/app)   (src/agent)    (subscription)
└─────────────┘                │                 │
                               │                 │  @tool calls
                               ▼                 ▼
                          allow-list      registry (405 tools)
                          + approvals     ├── google/* (drive, sheets, …)
                          (src/policy)    ├── russian/* (wb, ozon, …)
                                          └── infra/* (audit, kpi, mdm, …)
```

Каждый tool-call проходит через:
- **policy gate** — allow-list или запрос подтверждения у юзера
- **idempotency** — Stripe-style key для retry-safe destructive операций
- **dry_run** — preview без выполнения для всех destructive tools
- **OTel span + Prometheus counter** — observability
- **error classifier** — единая таксономия error_kind для агента

## Структура

```
src/
├── app.py                 FastAPI + SSE + Auth + RBAC middleware
├── agent.py               ClaudeSDKClient + policy/approval bridge
├── auth.py                Multi-account Google OAuth
├── auth_oidc.py           OIDC JWT verifier (Authentik / Keycloak)
├── tenancy.py             Per-tenant ContextVar middleware
├── mcp_http.py            MCP Streamable HTTP transport
├── metrics.py             Zero-dep Prometheus text-emission /metrics
├── updater.py             Manifest check + download + self-replace
├── telegram_bot.py        Approval workflow + alert delivery
├── tools/
│   ├── registry.py        Single source of truth for tool name → handler
│   ├── _wrap_for_sdk.py   The middleware chain every tool flows through
│   ├── _vendor_http.py    Shared HTTP transport for vendor clients
│   ├── _idempotency.py    Per-tenant retry cache
│   ├── drive / sheets / docs / slides / gmail / calendar / forms / tasks / contacts
│   ├── wb / ozon / yamarket / moysklad / onec
│   ├── payments / edo / logistics / messaging
│   └── infra / service / verify / ...
deploy/
└── k8s/                   Single-replica K8s manifests
docs/
├── DISTRIBUTION.md        How to ship .exe to end users
├── PHASE_0_DEPLOY.md      Multi-tenant production setup runbook
└── HANDOFF.md             Full project state + phase history
```

## Lifetime

Open source. Contributions welcome — особенно интеграции с другими
российскими сервисами или новые инструменты Workspace.

Issues / feature requests: [GitHub Issues](https://github.com/nnenoix/Assistant/issues).

## Лицензия

MIT.
