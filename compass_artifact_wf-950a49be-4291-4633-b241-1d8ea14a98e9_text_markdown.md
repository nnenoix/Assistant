# Системные пробелы MCP-сервера Workspace Agent: от персонального помощника к командному сервису селлера маркетплейсов

## TL;DR

- **Главный диагноз**: Workspace Agent сегодня — это «персональный CLI-помощник на стероидах», а не сервис. Чтобы он стал командным, нужны **не ещё инструменты, а семь системных слоёв**: серверная плоскость (Streamable HTTP + queue + scheduler + webhooks), multi-tenancy/RBAC/audit, детерминированные ML/rules-прослойки (NER/normalize/match вместо «галлюцинаций»), observability+eval loop, российские интеграции (1С, ЭДО, банк, курьерка, СМС), долговременный data layer с lineage, UI для нетехнической команды.
- **Главная архитектурная развилка**: pure-MCP vs **hybrid (MCP + FastAPI)**. Рекомендация — гибрид: FastMCP подключается к FastAPI через `app.mount("/mcp", mcp.streamable_http_app())`. MCP-протокол остаётся контрактом, FastAPI приносит webhook ingress, cron, OAuth-фронт для команды, OpenAPI для админки.
- **Roadmap**: 6 фаз; **Фаза 0 — фундамент (4–6 недель)** обязательна до добавления новых tools из бэклога 126; MVP командного сервиса собирается за ~12–18 недель, бюджет инфраструктуры для команды 5–10 человек — ~25 000–35 000 ₽/мес на Yandex Cloud.

---

## Key Findings

1. **MCP-сервер без HTTP-плоскости — это не сервис, а CLI-плагин.** В Workspace Agent сейчас нет Streamable HTTP transport (рекомендованного спецификацией MCP с 2025-03-26), нет webhook-приёмника, нет планировщика, нет очередей. Любая долгая операция блокирует сессию Claude Code. Без перехода на hybrid (MCP + FastAPI) пункты б/в/г/д из исходного брифа физически невыполнимы.

2. **Детерминированные «руки» обязательны, иначе LLM каждый раз изобретает заново правила, которые должны быть кодом.** Для российской специфики стек уже сформирован: Natasha+Yargy (NER), DaData (адреса КЛАДР/ФИАС), Splink (entity resolution), multilingual-e5 (embeddings), Pandera (DataFrame validation), PaddleOCR (накладные), lxml+XSD ФНС (УПД 5.03). Без них агент будет «иногда» подставлять кривой ИНН в платёжку.

3. **Observability должна быть OpenTelemetry-first, не vendor-locked.** OpenTelemetry GenAI Semantic Conventions разрабатывается с апреля 2024 OpenTelemetry GenAI SIG — это будущий стандарт. Langfuse 3.x работает как OpenTelemetry Backend на `/api/public/otel`; Arize Phoenix построен полностью на OpenTelemetry с zero feature gates в self-hosted версии. Любой другой выбор — лок-ин.

4. **Multi-tenancy и identity нельзя «добавить потом».** MCP spec 2025 явно требует `<tenant_id>:<user_id>:<session_id>` формат и явный запрет на использование sessions для аутентификации. Path-based isolation (`/{tenant}/mcp`) — стандартный паттерн (Sage MCP, FastMCP Multi-Tenancy). Если открыть webhooks без identity — любой может прислать «order completed».

5. **Очередь должна быть async-first для LLM-нагрузки.** Celery «still has no native async/await support as of 2025», issue #6552 открыт с 2020. arq (от автора Pydantic) держит 50 параллельных LLM-вызовов в одном процессе вместо 50 worker-процессов у Celery — критично для I/O-bound LLM API.

6. **UI для команды — это отдельный фронт, не Claude Code.** LibreChat — первая платформа с полной поддержкой MCP (stdio/HTTP/SSE) и multi-user isolation, 4 ноября 2025 года приобретена ClickHouse. Open WebUI (~138 000 GitHub-звёзд на 21 мая 2026) — альтернатива с RBAC/SSO/OIDC/LDAP/SCIM 2.0. Для алертов и approval — Telegram bot (лимит 30 msg/sec на бота покрывает команду до сотен человек).

7. **Российская специфика требует своих интеграций как класса.** 1С (REST/OData), СБИС (СБИС.СписокДокументов/СБИС.СписокИзменений), Контур.Диадок, IMAP/SMTP корпоративной почты, SMS-шлюзы (SMS.ru `smsru_api` + SMSC.ru `smsc-python`), курьерка (СДЭК API v2.0 + Boxberry), ОФД, ЕДО, ЮKassa/Тинькофф Acquiring, Avito/VK.

8. **Data layer = Postgres (state) + DuckDB (аналитика).** PostgreSQL 16 для OLTP/RBAC/audit; DuckDB 1.4 LTS (октябрь 2025) с AES-256 и MERGE для аналитики — 9 октября 2025 года DuckDB занял #1 на ClickBench среди open-source систем в hot runs. ClickHouse — только если данных >100 М строк.

9. **Eval framework — DeepEval + Phoenix, осторожно с Promptfoo.** Promptfoo приобретён OpenAI 9 марта 2026 (финансовые условия не раскрыты; $86 млн — это post-money оценка Series A июля 2025), что ставит вопрос о вендор-нейтральности. DeepEval («Pytest для LLM», 60+ метрик) + Phoenix (4 agent-эвалуатора: function calling, path convergence, planning, reflection) — нейтральная альтернатива.

10. **Multi-agent (CrewAI/LangGraph) — это Фаза 4, а не старт.** «Teams that start with CrewAI for prototyping often migrate to LangGraph when they need production-grade state management». Сначала — стабильный single-agent сервис, потом разделение ролей (Финдир/Закупщик/Менеджер).

---

## Details

### I. Семь системных пробелов как классы

| Пробел | Без него | Класс решения |
|---|---|---|
| Multi-tenancy/identity/RBAC | Бухгалтер видит маржу, кладовщик правит цены, нельзя восстановить «кто что сделал» | Path-based tenant isolation + Authentik/Keycloak + Casbin |
| Серверная плоскость | Нет webhooks/cron/long-running, агент живёт только в чате | FastAPI + MCP Streamable HTTP + arq + Redis |
| ML/rules-прослойки | LLM «галлюцинирует» в нормализации и матчинге | Natasha+Yargy+DaData+Splink+Pandera+PaddleOCR |
| Observability + eval | Не понять, стал ли агент хуже, не понять costs/errors | OpenTelemetry GenAI + Langfuse/Phoenix + DeepEval + Sentry |
| Российские интеграции | Реальный селлер живёт в 1С, СБИС, банке, СДЭК | Отдельные tool-категории, не «универсальный API» |
| Data layer + lineage | Каждый раз тянем заново; нет MDM; нельзя ответить откуда цифра | Postgres + DuckDB + Alembic + master data tables |
| UI для команды | Нетехнические сотрудники не могут | LibreChat + Telegram bot + Superset + approval UI |

### II. Подробно по 10 областям

#### Область 1. ML/Rules-прослойки (P0)

| Назначение | Технология | Обоснование |
|---|---|---|
| Русский NER | **Natasha (Slovnet NER) + Yargy** | Модель ~30 МБ; per natasha.github.io/ner — «Качество на 1 процентный пункт ниже, чем у SOTA DeepPavlov BERT NER, размер модели в 75 раз меньше, потребление памяти в 30 раз меньше, скорость в 2 раза больше на CPU» |
| Адреса РФ | **DaData API** + `dadata-py` (hflabs/dadata-py) | КЛАДР/ФИАС; бесплатно 10 000 запросов/день; «Лёгкий» 14 000 ₽/год — 50 000 запросов/день; standardization 20 коп./запись с 13 января 2025 |
| Entity resolution | **Splink** (Fellegi-Sunter + DuckDB) | «Capable of linking a million records on a laptop in around a minute»; unsupervised |
| Дедупликация | **rapidfuzz** + sentence-transformers | Двухэтапно: blocking + embedding similarity |
| Embeddings локально | **intfloat/multilingual-e5-base ONNX QInt8** | Топ по ruMTEB benchmark; CPU-инференс через onnxruntime |
| Парсинг УПД (XML 5.03) | **lxml** + XSD ФНС | Приказ ФНС от 19.12.2023 № ЕД-7-26/970@, применяется с I кв. 2026 |
| OCR накладных | **PaddleOCR** + Tesseract (rus) + EasyOCR | PaddleOCR показал «статистически значимое превосходство в обработке сложных макетов и табличных данных» в исследовании на 2-НДФЛ |
| Классификатор писем | scikit-learn TF-IDF + LogReg или fasttext | 5 классов: поставщик/клиент/банк/маркетплейс/спам |
| DataFrame validation | **Pandera** | ~12 зависимостей, в несколько раз быстрее Great Expectations на 5М-строк datasets; class-based schemas похожи на Pydantic |

**Новые tool-категории**: `ner.*`, `normalize.*`, `match.*`, `embed.*`, `classify.*`, `validate.*`, `ocr.*`.

#### Область 2. Инфраструктура агента как сервиса (P0)

| Слой | Рекомендация | Обоснование |
|---|---|---|
| MCP transport | **Streamable HTTP** (`mcp.run(transport="streamable-http")`) | Спецификация MCP 2025-03-26 рекомендует для production; bi-directional через единый endpoint /mcp; load-balancing-friendly |
| HTTP framework | **FastAPI** через `app.mount("/mcp", mcp.streamable_http_app())` | Canonical pattern из официального python-sdk; вокруг MCP — webhooks/cron/admin/OAuth |
| Очередь | **arq** (LLM/IO) + Dramatiq (heavy sync) | arq async-native; Dramatiq для надёжности тяжёлых задач |
| Брокер | **Redis 7** | Уже нужен для cache/sessions; не вводим RabbitMQ зря |
| Scheduler | arq cron + APScheduler | Для DAG'ов — Prefect; для критичных бизнес-процессов — Temporal |
| Workflow engine | **Prefect** (data) / **Temporal** (financial, durable) | Temporal: «useful for multi-agent systems and human-in-the-loop AI», durable execution |
| Event bus | Redis Streams → NATS JetStream | Redis Streams хватает до ~10 М событий/день |
| Webhook ingress | FastAPI endpoint с idempotency-key + queue для retry | Паттерн Event Gateway (Hookdeck) — внешний gateway держит dedup, MCP получает чистые события |
| Multi-tenancy | Path `/{tenant_slug}/mcp` или header `X-Tenant-ID` | MCP spec: «Session ID format `<tenant>:<user>:<session>` to prevent cross-tenant access» |
| Distributed locks | Redis SETNX + TTL | Lock `lock:supply:WB:12345`; для критичных — Redlock |

**Триггеры**: `wb.new_order`, `wb.new_review`, `wb.stock_low`, `ozon.*`, `bank.new_payment`, `edo.new_document`, `mail.new_supplier_letter`, `schedule.daily_morning`, `schedule.weekly_p&l`.

**Новые tool-категории**: `job.*`, `schedule.*`, `event.*`, `lock.*`, `webhook.*`.

#### Область 3. Российские интеграции (P0/P1)

| Класс | Технологии Python | Конкретика |
|---|---|---|
| 1С | REST/OData (платформа автогенерирует) + requests + XML/JSON | Открытый Пакет Интеграций — open-source библиотека с примерами |
| МойСклад | REST API | Прямой REST или SDK |
| СБИС | СБИС.СписокДокументов / СБИС.ПрочитатьДокумент / СБИС.СписокИзменений | Для Требований — параметр `Тип=ИстребованиеФНС` в СБИС.СписокИзменений |
| Контур.Диадок | REST API | Контур API |
| Корпоративная почта | `imap-tools` + `email` (stdlib) | mail.ru, Я.Почта, Bitrix — все IMAP |
| SMS.ru | `smsru_api` (PyPI, async, до 100 номеров) | Цены: 25 коп/SMS при низком объёме; 7 коп/SMS при тратах >5 000 ₽/мес |
| SMSC.ru | `smsc-python` | Транзакционная МТС с 01.10.2025 — 66 ₽ за первое SMS в месяце на абонента, далее бесплатно |
| P1SMS | REST через requests | Каскадные рассылки (SMS→Viber→Telegram→WhatsApp); Telegram bots с inline-клавиатурой |
| Telegram (алерты команде) | `aiogram 3.x` | Лимит 30 msg/sec на бота, 1 msg/sec одному пользователю, 20 msg/min в группу; для команды <100 чел — за 4 сек |
| Печать этикеток | `zpl` библиотека / raw TCP 9100 | ZPL (Zebra) + TSPL (TSC); barcode generation через `python-barcode` |
| Сканирование штрихкодов | USB HID-сканеры эмулируют клавиатуру; для камер — `pyzbar` + opencv | — |
| ЮKassa/Тинькофф Acquiring/CloudPayments | REST + webhook callbacks | — |
| СДЭК v2.0 (OAuth2) | requests + OAuth | — |
| Boxberry | API-token | — |
| Календари (не Google) | `caldav` (Я.Календарь, Mail.ru); Microsoft Graph для Outlook | — |
| VK API / Avito | `vk_api`; для Avito — обратная разработка | — |
| OFD | Платформа ОФД / OFD.ru REST | — |

**Новые tool-категории**: `onec.*`, `moysklad.*`, `sbis.*`, `kontur.*`, `diadoc.*`, `mail.*`, `sms.*`, `print.*`, `courier.*`, `payment.*`.

#### Область 4. Observability (P0/P1)

| Слой | Рекомендация | Обоснование |
|---|---|---|
| Tracing format | **OpenTelemetry GenAI Semantic Conventions** | Стандарт OpenTelemetry GenAI SIG с апреля 2024; на март 2026 в experimental, но уже поддерживается Langfuse/Phoenix/Datadog/New Relic |
| LLM observability | **Langfuse 3.x self-hosted** | Работает как OpenTelemetry Backend через /api/public/otel OTLP endpoint; поддерживает OpenLLMetry/OpenLIT |
| Альтернатива | **Arize Phoenix self-hosted** | «Only major platform built entirely on OpenTelemetry with no proprietary tracing layer. Fully self-hostable with zero feature gates» (Goodeye Labs review); Elastic License 2.0 |
| Auto-instrumentation | OpenLLMetry (Traceloop) или OpenLIT | Auto-инструментит Anthropic SDK, OpenAI SDK, LangChain |
| Metrics | Prometheus + Grafana | `prometheus-fastapi-instrumentator` |
| Logs | structlog + Loki (или Yandex Cloud Logging) | JSON-логи; дешёвое хранение |
| Error tracking | Sentry self-hosted или GlitchTip (AGPL, Sentry-compatible) | — |
| Alerting | Grafana Alerting → Telegram bot | PagerDuty/Opsgenie недоступны в РФ |
| Health checks | FastAPI `/healthz`+`/readyz` + external API status broker | Раз в минуту пингует WB/Ozon/банк |

**SLO для команды селлера**: 99.5% uptime; p95 tool latency <5s (без LLM); p95 LLM tool <30s; cost per active user <N₽/день; 0 cross-tenant data leaks; 100% destructive ops c trace+audit.

**Новые tool-категории**: `observe.*`, `health.*`, `cost.*`.

#### Область 5. Eval / Dev loop (P0/P1)

| Слой | Рекомендация | Обоснование |
|---|---|---|
| Eval framework | **DeepEval** + **Phoenix agent evaluators** | DeepEval — «Pytest для LLM», 60+ метрик; Phoenix — 4 agent-эвалуатора (function calling, path convergence, planning, reflection) |
| Promptfoo предостережение | Использовать с риском вендор-лока | OpenAI приобрёл Promptfoo 9 марта 2026 (финансовые условия не раскрыты; цифра $86 млн — это post-money оценка Series A июля 2025, не цена сделки) |
| LLM-as-judge | DeepEval G-Eval с локальной моделью (Saiga/Vikhr/GigaChat) | Без OpenAI API |
| Mocking APIs | pytest-recording (VCR) + respx | Записываем WB-ответы один раз |
| Integration sandbox | testcontainers + ephemeral Google sheets/drive | CI-run создаёт временные ресурсы |
| CI/CD | GitHub Actions / Gitea Actions self-hosted + pre-commit (ruff/black/mypy/bandit) + commitizen + semantic-release | Conventional commits → автоверсии |
| Feature flags | Unleash или GrowthBook self-hosted | Раскатка нового tool на одного пользователя |
| Replay | Сохранение full trace в Langfuse → re-run | Critical для отладки прод-инцидентов |

**Особенность через Claude Code CLI**: пользователь работает без Claude API. LLM-as-judge запускается **через тот же Claude Code CLI в headless/SDK режиме**: открыть session → дать prompt с golden task → assert на выводе. Можно автоматизировать в CI через Claude Code SDK.

**Новые tool-категории**: `test.*`, `flag.*`, `dev.*`.

#### Область 6. Data layer (P0/P1)

| Слой | Рекомендация | Обоснование |
|---|---|---|
| OLTP | **PostgreSQL 16** | Managed в Yandex Cloud; pgvector для embeddings; pg_partman для time-partitioning |
| OLAP | **DuckDB 1.4 LTS** (октябрь 2025) | AES-256, MERGE, Iceberg writes; per duckdb.org/2025/10/09 «On October 9, 2025, DuckDB's in-memory variant hit #1 on the popular ClickBench database benchmark»; после изменения правил 26 октября — #1 среди open-source в hot runs, уступая только закрытому Umbra |
| OLAP при >100М строк | ClickHouse | На больших JSON в бенчмарках может быть многократно быстрее DuckDB/Postgres |
| Migrations | Alembic + DuckDB `__migrations` table | — |
| ORM/DAL | SQLAlchemy 2.0 или SQLModel | SQLModel — от автора FastAPI, Pydantic-совместимый |
| Backup | pg_dump cron + S3 (Yandex Object Storage) | RPO 24h, RTO <1h |
| ETL | dlt или pure Python + Pandera | dlt — declarative ELT с DuckDB/Postgres destinations |
| Lineage | OpenLineage → Marquez self-hosted | Опционально на P2 |
| MDM | Таблицы products / suppliers / contractors + external_ids mapping | Single source of truth + сопоставление через entity resolution |
| PII (152-ФЗ) | PII-теги в схеме + RBAC mask в serialization + аудит согласия | — |

**Новые tool-категории**: `db.*`, `mdm.*`, `lineage.*`, `backup.*`.

#### Область 7. UI для команды (P0/P1)

| Цель | Что брать | Почему |
|---|---|---|
| Чат для команды | **LibreChat** self-hosted (MIT) | First-class MCP support (stdio/HTTP/SSE), multi-user isolation; LDAP/SSO/Azure AD/Discord/GitHub OAuth + автомодерация; 4 ноября 2025 г. приобретён ClickHouse (per clickhouse.com/blog: «SAN FRANCISCO—November 4, 2025—ClickHouse, Inc. announced the acquisition of LibreChat») |
| Альтернатива | Open WebUI | ~138 000 GitHub-звёзд на 21 мая 2026; RBAC, SSO/OIDC/LDAP, SCIM 2.0, analytics, Python extensibility c MCP |
| Веб-дашборды | Apache Superset | OSS; коннект к Postgres/DuckDB/ClickHouse |
| Telegram-фронт | aiogram 3.x bot + MCP client | Уведомления + базовые read-only команды + approval кнопки |
| Approval UI | Telegram inline buttons или web-кнопка в LibreChat artefacts | tool `request_approval(payload)` → агент ждёт → callback → продолжает |
| Mobile | PWA от LibreChat/Open WebUI | Не нативное Android/iOS |

**Архитектура UI**:
- LibreChat — главный фронт для бухгалтера/закупщика/менеджера
- Claude Code CLI остаётся для разработчика
- Telegram — алерты + быстрый approve/reject + read-only queries
- Superset — еженедельные/ежемесячные отчёты

**Новые tool-категории**: `approval.*`, `notify.*`, `dashboard.*`.

#### Область 8. Security командного режима (P0)

| Слой | Рекомендация | Обоснование |
|---|---|---|
| Secrets manager | **Infisical** self-hosted (MIT) или **OpenBao** (community-fork Vault, MPL 2.0, под Linux Foundation) | «If licensing is a showstopper, favor Infisical or OpenBao over Vault's Business Source License» (Infisical blog); ESO для Kubernetes-sync; SOPS+age для лёгких проектов |
| AuthN | OAuth 2.1 (MCP-spec 2025) + SSO через Authentik/Keycloak | MCP 2025 spec требует OAuth 2.1 для remote servers |
| AuthZ/RBAC | Casbin (Python OSS) или Postgres roles/permissions | Casbin поддерживает RBAC/ABAC/RESTful |
| Audit log | Append-only `audit_log(user, tool, args_hash, ts, result_status)` в Postgres → ротация в DuckDB/S3 | Отдельно от обычных логов (PII risk) |
| Approval workflow | tool `request_approval` + Temporal или простой Redis-based + Telegram callback | Critical для финансов |
| Session management | JWT TTL 15 мин + refresh token | MCP spec: «Sessions MUST NOT be used for authentication (use tokens instead)» |
| Field-level masking | RBAC роль → mask filters в API gateway | role=accountant → tool response убирает `margin`, `cost_price` |

**Операции с двумя подтверждениями (минимум)**: платёж >X₽; обновление цен >10%; массовая рассылка >100 SMS; изменение карточки товара с >Y продаж/мес; удаление справочного объекта.

**Новые tool-категории**: `auth.*`, `audit.*`, `rbac.*`.

#### Область 9. Multi-agent (P2)

| Сценарий | Что брать |
|---|---|
| Сложные durable workflows | **LangGraph 1.0** (конец 2025) — default runtime для всех LangChain agents; durable execution с built-in checkpointing и time travel; human-in-the-loop; лидер по поисковому объёму |
| Командная коллаборация по ролям | **CrewAI** — «20 lines to start», role-based DSL |
| Conversational debate | AutoGen / AG2 — GroupChat паттерн |
| Универсальный durability | Temporal оборачивает агента |

**Рекомендация для селлера**: CrewAI как старт (Финдир / Закупщик / Менеджер / Ассистент), LangGraph для критичных процессов (закупка, претензии с retry и human approval). Один общий MCP-сервер как tool layer для всех агентов.

**Новые tool-категории**: `agent.*`, `bb.*` (blackboard), `handoff.*`.

#### Область 10. Deployment (P0/P1)

| Слой | Рекомендация |
|---|---|
| Container | Docker multi-stage + **uv** (10–100× быстрее pip) |
| Compose | docker-compose → K3s при росте |
| Hosting | **Yandex Cloud** (ФСТЭК, managed Postgres/Redis/MQ) для критичного + Selectel S3 для backup |
| Config | pydantic-settings + env-specific YAML |
| Versioning | SemVer 2.0 + conventional commits + semantic-release |
| Migrations | Alembic в release-pipeline |
| Updates | Blue/green через nginx upstream + idempotent migrations |
| Rollback | Tag previous image + `docker compose up <tag>` |
| DR | Yandex Cloud snapshots + S3 cross-region |

**Новые tool-категории**: `ops.*`.

---

## III. Итоговый Roadmap (свод всех трёх документов)

### Фаза 0 — Фундамент (4–6 недель) — **ОБЯЗАТЕЛЬНА до бэклога 126**

1. FastAPI + Streamable HTTP MCP transport
2. Identity + multi-tenancy + RBAC (Postgres + Authentik + Casbin)
3. Infisical/SOPS для секретов
4. Postgres + Alembic + pydantic-settings + базовый backup в S3
5. Audit log для всех destructive операций
6. structlog + Sentry для базовой observability
7. Docker + docker-compose
8. pytest + pre-commit + ruff + mypy

### Фаза 1 — Командный сервис MVP (6–8 недель)

1. arq + Redis + scheduler + webhook ingress endpoints
2. **LibreChat** self-hosted + Streamable HTTP к MCP + OAuth Authentik
3. **Telegram bot** для команды (алерты + read-only + approval)
4. **DaData** интеграция — первый детерминированный «руки»-слой
5. Distributed locks (Redis SETNX) на критичные ресурсы
6. **OpenTelemetry GenAI** инструментирование + Langfuse self-hosted
7. Из бэклога 126: подмножество marketplace ops (только read + safe write: остатки, отзывы, продажи) под новый transport
8. Из 236 улучшений: применить shared infrastructure (retry/idempotency/dry_run/audit) ко всему

### Фаза 2 — ML/Rules-прослойки (4–6 недель)

1. **Natasha + Yargy** (ИНН/КПП/БИК/штрихкоды)
2. **multilingual-e5-base ONNX** + usearch/FAISS локальный индекс
3. **Splink** + **rapidfuzz** для entity resolution
4. **Pandera** schemas для всех ETL
5. **PaddleOCR** + Tesseract + EasyOCR для бумажных накладных
6. Классификатор писем (sklearn TF-IDF + LogReg)
7. **MDM** таблицы products/suppliers/contractors с external_ids
8. **DuckDB** для аналитики

### Фаза 3 — Интеграции (8–12 недель параллельно)

1. **1С** (OData/HTTP-сервисы) или **МойСклад** REST
2. **IMAP/SMTP** для писем поставщиков (`imap-tools`)
3. **SMS-шлюз** (SMS.ru `smsru_api` или SMSC.ru `smsc-python`)
4. **СДЭК + Boxberry** API + Почта России
5. **ZPL/TSPL** печать этикеток
6. **ЮKassa / Тинькофф Acquiring** webhooks
7. **СБИС / Контур.Диадок** (ЭДО)
8. **Avito API + VK API**
9. Из 126: добить marketplace ops (38), финансы/юнит-эконом (16), CRM/документы/ЭДО (12)

### Фаза 4 — Multi-agent + Advanced (4–6 недель)

1. **CrewAI** — Финдир/Закупщик/Менеджер/Ассистент
2. **LangGraph** для критичных процессов
3. **Temporal** для durable финансовых workflow
4. Full approval workflows
5. Из 126: orchestration (6) — policy engine, skill registry, code-exec sandbox, BI dashboard, escalation router
6. A/B testing промптов через Phoenix/GrowthBook
7. **DeepEval + Phoenix** golden tasks в CI

### Фаза 5 — Масштабирование (по необходимости)

1. ClickHouse если данных >100 М строк
2. NATS JetStream если несколько микросервисов
3. K3s если несколько узлов
4. Lineage (Marquez/OpenLineage) при регуляторике
5. Geographic redundancy для многофилиальных компаний
6. Field-level encryption для финансов

---

## IV. Ключевые архитектурные решения

### Решение 1. Pure-MCP vs Hybrid (MCP + FastAPI)

| Опция | Pros | Cons |
|---|---|---|
| Pure MCP | Минимум кода | Нет webhooks/cron/UI кроме чата |
| **Hybrid MCP+FastAPI** ⭐ | MCP tools переиспользуются как core; FastAPI приносит webhooks/cron/admin/UI/OAuth; canonical pattern из python-sdk | Чуть больше boilerplate |

### Решение 2. Очередь

| Опция | Pros | Cons |
|---|---|---|
| Celery | Зрелая, любой broker | Нет native async/await как 2025 (issue #6552 c 2020); сложная |
| **arq** ⭐ | Async-native, от автора Pydantic; 50 параллельных LLM-вызовов в одном процессе | Маленькая экосистема |
| Dramatiq | Лучшая reliability | Меньше комьюнити |
| RQ | Простейшая | Sync only |

### Решение 3. Database

| Опция | Применение |
|---|---|
| **Postgres 16** ⭐ | OLTP, state, users, RBAC, audit |
| **DuckDB 1.4 LTS** ⭐ | OLAP, аналитика, отчёты |
| ClickHouse | Только при >100М строк |

### Решение 4. Auth

| Опция | Pros | Cons |
|---|---|---|
| **Authentik** ⭐ | OAuth 2.1, OIDC, легче в эксплуатации | Молодой |
| Keycloak | Зрелый, enterprise | Тяжёлый |

### Решение 5. UI

| Опция | Pros | Cons |
|---|---|---|
| **LibreChat** ⭐ | First-class MCP; multi-user isolation; SSO/LDAP; приобретён ClickHouse 4 ноября 2025 | Не enterprise knowledge platform |
| Open WebUI | ~138 000 GitHub-звёзд на 21.05.2026; RBAC/SSO/SCIM | Менее многопровайдерный |
| Onyx | Enterprise search, 40+ connectors | Overkill для селлера |

### Решение 6. Tracing

| Опция | Pros | Cons |
|---|---|---|
| **Langfuse self-hosted** ⭐ | OpenTelemetry backend; prompt management | Не purely OTel-native |
| **Arize Phoenix** ⭐ | Полностью на OpenTelemetry, zero feature gates в self-hosted | Elastic License 2.0 |
| LangSmith | Богатый | Vendor lock, LangChain dependency |

### Решение 7. Hosting

| Опция | Pros | Cons |
|---|---|---|
| **Yandex Cloud** ⭐ | ФСТЭК, managed Postgres/ClickHouse/Redis/MQ | Цены выше Selectel |
| Selectel | Дёшево, S3, ФСТЭК | Меньше managed services |
| SberCloud / VK Cloud | ФСТЭК, managed | Меньше популярны |

---

## Recommendations

**Стартуйте здесь, в этом порядке:**

1. **На этой неделе**: Заморозьте добавление новых tools из бэклога 126. Создайте отдельный репозиторий «Workspace Agent v2» с FastAPI+FastMCP-скелетом и Streamable HTTP transport (`app.mount("/mcp", mcp.streamable_http_app())`).

2. **Месяц 1**: Соберите Фазу 0. PostgreSQL для state/users, Authentik для OAuth, Casbin для RBAC, Infisical для секретов, structlog+Sentry, Docker. Это «командный сервис в зародыше» — пусть пока без новых tools, но multi-user.

3. **Месяцы 2–3**: Фаза 1. Подключите arq+Redis, поставьте LibreChat self-hosted и Telegram bot, добавьте DaData (это уже первый явный «руки»-слой и заметный win для команды), включите OpenTelemetry+Langfuse. На этом этапе подключите подмножество safe-tools маркетплейсов к новому транспорту.

4. **Месяц 4**: Фаза 2 (детерминированные руки). Natasha+Yargy, multilingual-e5, Splink, Pandera — выкатывайте одну подсистему в неделю.

5. **Месяцы 5–7**: Фаза 3 параллельно — берите по 2 интеграции одновременно: 1С + IMAP/SMTP, потом СДЭК + ЮKassa, потом СБИС + ZPL-печать.

6. **Дальше — по событиям**: Фаза 4 (multi-agent) только после стабильного single-agent; Фаза 5 — по триггерам объёма.

**Триггеры для пересмотра решений** (бенчмарки):

| Если… | …то |
|---|---|
| >100 М строк в аналитике | Переходим с DuckDB на ClickHouse |
| >10 М событий/день | Redis Streams → NATS JetStream |
| >3 независимых deployment'а | docker-compose → K3s |
| >50 одновременных пользователей | Sticky sessions критичны, нужен Redis-based session store + load balancer config |
| Регуляторика 152-ФЗ становится критичной | Добавляем OpenLineage/Marquez, шифрование PII полей |
| Promptfoo меняет лицензию или vendor-locks под OpenAI | Полностью переключаемся на DeepEval+Phoenix |
| Стоимость DaData > 50 000 ₽/мес | Рассмотреть локальный КЛАДР dump + Sphinx/Elasticsearch для поиска |

---

## Caveats

### Чего НЕ делать (антипаттерны)

1. **Не строить свой агентный фреймворк** — LangGraph + CrewAI существуют, не переизобретайте checkpointing и role-based orchestration.
2. **Не делать pure MCP без HTTP+webhook** — получите масштабируемого только горизонтально по добавлению tools, не по пользователям.
3. **Не ставить Kubernetes на старте** — docker-compose покроет команду 5–10 человек.
4. **Не использовать pickle сериализацию в очереди** — «major security vulnerability if an attacker can inject data into your queue». Только JSON.
5. **Не складывать секреты в .env.example в Git** — это сигнал атакующему. SOPS/Infisical обязательны.
6. **Не делать LLM-as-judge без бюджета** — известный случай: AI agent в fintech-компании в марте 2025 вошёл в runaway loop при reconciliation, работал 11 дней, накопил $47 000 расходов (приведено как пример в Goodeye Labs research on agent evaluation tools).
7. **Не делать multi-agent до стабильного single-agent** — «teams that start with CrewAI for prototyping often migrate to LangGraph when they need production-grade state management».
8. **Осторожно с Promptfoo долгосрочно** — приобретён OpenAI 9 марта 2026 (финансовые условия не раскрыты), нейтральность под вопросом.
9. **Не делать Great Expectations для small data validation** — overkill; Pandera значительно быстрее на типичных датасетах.
10. **Не отдавать MCP-сессию через несколько балансировщиков без sticky** — «if you use multiple webhook replicas, route all /mcp* requests to a single, dedicated webhook replica» (n8n docs).

### Где переусложнить легко

- **Temporal** — мощный, но операционно дорогой. На старте arq + idempotent retries.
- **NATS JetStream** — Redis Streams хватает до 10 М событий/день.
- **dbt** — для DuckDB-аналитики селлера достаточно SQL views + dlt.
- **Kafka** — категорически overkill.
- **OpenLineage/Marquez** — только при регуляторике.
- **Field-level encryption на всём** — достаточно encryption-at-rest + RBAC masks.

### Бюджет (ориентир, ₽/мес для команды 5–10 человек, Yandex Cloud)

| Компонент | Стоимость |
|---|---|
| Managed Postgres (small) | 3 000–5 000 |
| Managed Redis | 1 500–3 000 |
| Object Storage (backups) | 500–1 500 |
| VM 4 vCPU/8 GB (приложение) | 3 000–5 000 |
| VM 4 vCPU/8 GB (воркеры) | 3 000–5 000 |
| VM (observability) | 3 000–5 000 |
| DaData «Лёгкий» | ~1 200 (14 000/год) |
| SMS-шлюз (~3 000 SMS) | ~7 000 |
| Резерв | 2 000 |
| **Итого инфраструктура** | **~25 000–35 000 ₽/мес** |

Без LLM-токенов (подписка Claude Code у пользователя, ~20–200$/мес на разработчика).

### Компромиссы команды

- **Approval flows на простых задачах раздражают** — делайте только на destructive/financial операциях.
- **Audit log тормозит** — +50–100ms на write. Стоимость compliance.
- **Multi-tenant изоляция замедляет** — JOIN с tenant_id везде; SET search_path на tenant schema помогает.
- **CI eval каждый PR** — 10–30 мин; делайте только pre-merge.
- **LLM-as-judge стоит денег** — даже при self-hosted (Saiga/Vikhr/GigaChat) занимает GPU. Только на изменённых сценариях.
- **Observability ест диск** — Langfuse traces до сотен ГБ/мес. Retention: 30 дней detailed, 1 год aggregated.

### Финальная мысль

Workspace Agent **не стоит развивать «вширь»** (ещё 100 tools), пока не построены семь системных слоёв. Без них даже идеальный набор tools — это сильный персональный CLI-помощник, а не сервис, к которому может параллельно обращаться команда. Фаза 0 — это **обязательное условие** для всего остального. Соотношение трудозатрат на работающую систему: примерно 30% — системные пробелы из этого документа, 40% — интеграции (бэклог 126 + Российские), 30% — улучшения существующих tools (бэклог 236 + shared infrastructure).