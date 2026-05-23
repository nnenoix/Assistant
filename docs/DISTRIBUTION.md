# Distribution guide: «отдал .exe — работает»

Полный путь от «у меня код на машине» до «знакомый кликнул дважды и пишет в чат».

## TL;DR

1. **Один раз** создай Google OAuth client + Anthropic developer-id.
2. Положи `client_secret_*.json` в корень репо.
3. `git tag v0.2.0 && git push origin v0.2.0` → GitHub Actions собирает `.exe`.
4. Скинь знакомому ссылку на GitHub Release. Он скачивает, запускает,
   проходит wizard за 2 клика.

После этого все апдейты летят автоматом — поднял тег, юзер видит баннер
«доступна новая версия», нажал «Обновить», перезапустил.

---

## Один раз: Google OAuth client (бесплатно)

Этот шаг ты делаешь **один** для всех будущих юзеров. Они получат
твой client_id / client_secret — это нормально для desktop OAuth (PKCE
flow); у каждого юзера свой refresh_token, ты их не увидишь.

### 1. Создать проект в Google Cloud Console

https://console.cloud.google.com/

- Кнопка «Создать проект» (бесплатно, лимиты огромные).
- Запомни project_id — он потом понадобится для Cloud Logging / scripts.run.

### 2. Включить API

«APIs & Services» → «Library». Включить:

- Google Drive API
- Google Sheets API
- Gmail API
- Google Calendar API
- Google Docs API
- Google Slides API
- Google Forms API
- Apps Script API
- Drive Activity API
- Cloud Logging API
- People API (Contacts)
- Tasks API

(Всё бесплатно. Quotas: 1B запросов/день на проект — хватит на сотни юзеров.)

### 3. OAuth consent screen

«APIs & Services» → «OAuth consent screen»:

- **User Type**: External (если ты НЕ в Google Workspace).
- **App name**: Workspace Agent (или своё).
- **Scopes**: добавь все из `src/config.py:SCOPES` (Drive, Sheets, Gmail и т.д.).
- **Test users**: пока ты НЕ прошёл verification, можешь добавить до
  100 email'ов сюда. Только они смогут логиниться без warning'а.
- **Publishing status**: **Testing**. Не нажимай «Publish App» пока не
  готов к verification (см. ниже).

### 4. Создать OAuth client (Desktop application)

«APIs & Services» → «Credentials» → «Create Credentials» → «OAuth client ID»:

- **Application type**: **Desktop application** (важно — это PKCE flow,
  не нужен redirect URI и client_secret реально публичный).
- **Name**: что угодно.
- Кнопка **Download JSON** → получишь файл
  `client_secret_<long_id>.apps.googleusercontent.com.json`.

### 5. Положи рядом с репозиторием

```
D:\Google work\
├── client_secret_<...>.apps.googleusercontent.com.json   ← вот тут
├── src/
├── static/
├── workspace_agent.spec
└── ...
```

`.gitignore` уже исключает `client_secret_*.json` (см. строку 8) —
файл НЕ попадёт в коммиты.

PyInstaller подхватит файл из spec'а (`workspace_agent.spec` имеет
`datas=[("client_secret_*.json", ".")]` или аналог — проверь). При
билде .exe файл попадёт **внутрь** бинаря, юзер ничего не делает.

---

## Распространение

### Вариант A: GitHub Releases (рекомендую)

Уже всё настроено через `.github/workflows/release.yml`:

```bash
# 1. Поменяй версию в pyproject.toml
sed -i 's/version = "0.1.0"/version = "0.2.0"/' pyproject.toml

# 2. Закоммить + создай тег
git commit -am "release: v0.2.0"
git tag v0.2.0
git push origin master v0.2.0

# 3. GitHub Actions сам:
#    - Прогоняет тесты
#    - Билдит .exe (PyInstaller на windows-latest runner)
#    - Считает SHA-256
#    - Создаёт manifest.json
#    - Аплоадит и .exe и manifest на Release
```

Через ~10 минут на странице
`https://github.com/<owner>/<repo>/releases/latest` лежат:

- `workspace_agent.exe` (~150-200 MB)
- `manifest.json`:
  ```json
  {
      "latest_version": "0.2.0",
      "download_url": "https://.../workspace_agent.exe",
      "sha256": "...",
      "release_notes_url": "https://.../releases/tag/v0.2.0"
  }
  ```

**Скидываешь знакомому ссылку на `.exe`.** Он качает, запускает.

### Вариант B: через любой другой хостинг

Залей .exe куда угодно (Dropbox, S3, личный сервер). Сделай
manifest.json руками с тем же форматом + опубликуй по фиксированному
URL. Установленные .exe будут проверять этот URL и видеть обновления.

---

## Auto-update

Чтобы установленный .exe знал откуда тянуть апдейты, ему нужен env
`UPDATE_MANIFEST_URL`. Два способа:

### A. Bundled в .exe (рекомендую)

Добавь в `workspace_agent.spec` или `src/config.py` строку:

```python
import os
os.environ.setdefault(
    "UPDATE_MANIFEST_URL",
    "https://github.com/<owner>/<repo>/releases/latest/download/manifest.json",
)
```

Юзер ничего не настраивает.

### B. Через .env рядом с .exe

```ini
# .env (юзер кладёт в папку с .exe)
UPDATE_MANIFEST_URL=https://github.com/<owner>/<repo>/releases/latest/download/manifest.json
```

Менее удобно — юзер должен знать про .env.

---

## Что видит знакомый при первом запуске

1. **Двойной клик на `.exe`** → откроется окно (pywebview) с wizard'ом.
2. **Шаг 1: Установить Claude Code** — кнопка → PowerShell ставит Claude CLI
   через `irm anthropic.com/install.ps1 | iex`. ~30-90 сек.
3. **Шаг 2: Войти в Anthropic** — кнопка открывает терминал с
   `claude setup-token`. Юзер логинится через Pro/Max аккаунт.
4. **Шаг 3: Войти в Google** — кнопка открывает браузер с Google OAuth.
   Юзер выбирает аккаунт + подтверждает scopes.
5. **Готово.** Wizard уходит, открывается чат. Знакомый может писать.

Если на шаге 3 он видит warning «App not verified» — это значит, что
его email НЕ в Test users списке (см. шаг 3 OAuth consent screen выше).
**Добавь его в Test users** и попроси повторить.

---

## Когда нужен OAuth verification

Google требует verification если:

- У тебя >100 юзеров (для External + Testing-режима лимит).
- Хочешь убрать «App not verified» warning у новых юзеров.
- Используешь sensitive / restricted scopes (Gmail полный доступ,
  Drive полный доступ — да, мы используем).

### Процесс (бесплатно, ~1-4 недели)

1. На OAuth consent screen нажми **«Publish App»**.
2. Google сообщит какие шаги нужны:
   - Privacy policy URL (можно бесплатно на GitHub Pages).
   - Terms of service URL.
   - YouTube видео-демо как app использует sensitive scopes.
   - **Domain verification** — нужен подтверждённый домен.
   - **Security assessment** — для restricted scopes (Gmail, Drive),
     это самый дорогой шаг, может требовать аудит ($$$).
3. После approval — анлим юзеров, warning исчезает.

**Пока ты в Testing**: до 100 Test users без warning'а, после warning'а
любой может всё равно continuely-через "Advanced → Go to (unsafe)" —
но это плохой UX. Лучше держать Test users список в актуальном состоянии.

---

## Альтернатива: каждый юзер ставит свой OAuth client

Если не хочешь возиться с verification и/или раздавать одному client'у
неограниченное число юзеров — попроси каждого юзера создать **свой**
OAuth client (шаги 1-4 выше) и положить рядом со своим .exe. Тогда:

- Лимит 100 Test users → каждый сам себе testuser → 1 юзер всегда покрыт.
- Никаких твоих токенов или ответственности.
- Минус: usability — каждый юзер должен пройти Google Cloud Console
  ритуал. **Не подходит для нетехнических знакомых.**

`src/config.py:_find_client_secret` ищет файл в двух местах:
1. Рядом с .exe (юзерский client).
2. Внутри bundle (bundled-by-PyInstaller, твой).

Если юзер дропнул свой файл рядом — он перебивает bundled. Можешь
использовать это как «advanced override».

---

## Чек-лист перед раздачей

- [ ] OAuth client создан, JSON в репозитории.
- [ ] Все нужные API включены в Google Cloud project.
- [ ] OAuth consent screen заполнен, Test users добавлены.
- [ ] `UPDATE_MANIFEST_URL` зашит в код или в .env шаблон.
- [ ] `workspace_agent.spec` собирает .exe чисто (`uv run pyinstaller workspace_agent.spec --clean --noconfirm`).
- [ ] Тег `v0.X.0` запушен, GitHub Actions release workflow прошёл.
- [ ] Скачал .exe из Release, запустил на чистой машине — wizard работает.
- [ ] Поменял версию, пушнул новый тег → старый .exe показал баннер «доступна новая версия».
