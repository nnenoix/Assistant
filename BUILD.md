# Сборка `.exe`

## Команда

```bash
uv run pyinstaller workspace_agent.spec --noconfirm
```

Время сборки: ~50 сек. Результат: `dist/workspace_agent/` (~390 МБ).

## Что нужно положить в `dist/workspace_agent/` после сборки

1. **`client_secret_*.apps.googleusercontent.com.json`** — OAuth-клиент
   из Cloud Console. **Не бандлится** в exe (нельзя выкладывать creds).
   Скопируй из корня проекта рядом с `workspace_agent.exe`.

2. **`.data/`** — создастся автоматически при первом запуске. Содержит:
   - `tokens/` — OAuth-токены аккаунтов (после ре-OAuth)
   - `chats/` — история чатов
   - `notes.json`, `people.json` — память агента
   - `reports/`, `alerts.json`, `bound_scripts.json` — данные

   Если хочешь перенести существующее состояние — скопируй
   `D:\Google work\.data\` в `dist\workspace_agent\.data\`.

## Что НЕ бандлится — должно быть на машине отдельно

| Зависимость | Зачем | Как поставить |
|---|---|---|
| `claude` CLI | `claude-agent-sdk` shellит в `claude`, а не использует API-ключ | `npm i -g @anthropic-ai/claude-agent-sdk` (или `claude code` оффициальный установщик) |
| Microsoft Edge или Chrome | Playwright драйвит их (msedge → chrome → bundled Chromium) | Edge встроен в Windows 10/11, иначе ставится из Microsoft Store |
| WebView2 Runtime | `pywebview` рисует окно через WebView2 | Встроен в Win10 21H2+ и Win11; иначе [download](https://developer.microsoft.com/en-us/microsoft-edge/webview2/) |
| Интернет | Google API + WB + Anthropic + Cloud Logging | — |

## Что осознанно НЕ включили в бандл (для лёгкости)

- `torch`, `transformers`, `sentence-transformers` — нужны для
  семантического поиска по чатам/нотам. При отсутствии модуль
  `src.embeddings` graceful fallback на substring match (не падает).
  Если хочешь полноценный semantic search в exe-сборке — убери из
  `excludes` в `workspace_agent.spec` (+~700 МБ к бандлу).

- `tkinter` — не используем (UI на pywebview/WebView2).

## Запуск

Двойной клик по `workspace_agent.exe`. Откроется окно с чат-агентом.

Сервер uvicorn слушает `127.0.0.1:8765` — можно открыть в любом
браузере параллельно (`/accounts` для управления OAuth-токенами).

## Иконка / название

`workspace_agent.spec`:
- `name="workspace_agent"` → меняет имя файла и папки `dist/`
- `icon="path/to/icon.ico"` → добавляет иконку (сейчас не задано)
- `console=False` → нет чёрной cmd-консоли при запуске

## Где ловить ошибки

`console=False` скрывает stdout/stderr — для отладки:
- Запусти `workspace_agent.exe` из терминала
- ИЛИ временно поставь `console=True` в spec → пересобери

## Размер можно урезать

- UPX-сжать (риск false-positive AV): `upx=True` в spec
- Удалить `googleapis_discovery_cache` JSON-словари: они весят, но
  безопасно убираемы если ты не используешь редкие API
- Убрать `pdfminer` celery-style локали
