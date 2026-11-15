# ModelPulse

**In plain words:** you connected a bunch of custom models to your agent (your own providers, your
own API keys). ModelPulse tells you which of them are actually answering right now — and which are
down — so you can switch before you hit an error.

Model availability monitor for your AI agent's BYOK providers, with an on-demand MCP tool, a web
dashboard, and a one-shot HTML report.

![ModelPulse results table](docs/screenshot-table.png)

---

## English

### What it is

**In one sentence:** it sends each of your models a tiny real request and shows which ones answer,
so you stop guessing.

This is about **your own (custom) providers** — the ones you added yourself with an API key, not the
built-in models the agent ships with. Those stock models are usually already flagged as available;
the ones you wired up on your own are the ones that quietly go down, and nothing tells you. That is
what ModelPulse checks.

Your agent keeps its connected providers and models in a settings file — by default
`~/.qoder/settings.json` → `providers`.
ModelPulse reads **the same file** and actively probes every model with a `chat/completions`
request ("reply with one word: pong"), verifying that **real answer text** came back, not just
HTTP 200. No IPC, no plugin — the shared registry file is the whole integration contract.

Statuses:

- **REPLY** — text came back (`pong`);
- **EMPTY** — 200 but no content (reasoning only / empty `content`);
- **FAIL** — network/HTTP error (403, 404, 429, timeout, ...).

### Modes

1. **One-shot run** — `once.cmd`. Probes every model, writes an HTML report and **opens it in a
   separate browser window**; the console window may be closed — the report stays on disk.
2. **Web monitor** — `start.cmd` runs a local server (`127.0.0.1:8765`) with a dashboard: cards,
   filter, per-model history, 1h/24h uptime, interval re-scan. Two separate buttons drive checks:
   **Scan once** runs a single pass and stops; **Continuous scan** turns the re-scanning loop on or
   off. The monitor starts idle — nothing is probed until you press one of them (the interval field
   is the pause between passes in continuous mode). A running check can be aborted with **Esc** or
   the "Cancel" button; a cancelled pass is dropped entirely and never reaches history or
   statistics, and switching continuous mode off lets the current pass finish instead of killing it.
3. **MCP tool** — `mcp_server.py` exposes a tool the agent can call to run the same pass on demand.

### Interface language and theme

The dashboard and the report are bilingual: **RU / EN**. Toggle via the `RU | EN` segment in the
top-right corner of the dashboard; the choice is stored in `config.json → language` and applies to
the next `once.py` report. One-off override: `python once.py --lang en`.

The dashboard also switches **theme**: ☀ light, ☾ dark, 🖥 follow the system. The choice is kept in
`localStorage` (`mp-theme`) for instant application on the next load and mirrored to
`config.json → theme`, so a fresh browser picks up your last choice. "Follow the system" tracks the
OS live.

### Requirements

- Python 3.10+ (tested on 3.14, Windows and Linux).
- Nothing else: HTTP via stdlib `urllib`, storage via stdlib `sqlite3`.

### Quick start

```bash
python once.py                 # one pass + report in the browser
python once.py --filter groq   # only matching models
python once.py --no-open       # render the report, do not launch a browser
python server.py --port 8765   # web monitor at http://127.0.0.1:8765
```

On Windows, double-click `once.cmd` / `start.cmd` instead.

### Configuration

`config.json` next to the scripts:

```json
{
  "settings_path": "",
  "interval_seconds": 900,
  "max_tokens": 4096,
  "timeout_seconds": 30,
  "workers": 8,
  "language": "ru",
  "theme": "system",
  "browser": ""
}
```

- `settings_path` — path to the settings file; empty = `~/.qoder/settings.json`.
- `browser` — what opens the report: a full `.exe` path **or** the words
  `firefox` / `chrome` / `edge`. Empty = the Windows default browser.
- `workers` — how many models to probe in parallel.
- `max_tokens` — reply budget for the probe. Reasoning models spend tokens thinking before they
  emit text, so a small value makes them look dead: keep it generous (4096 by default).

The browser is chosen in this order: `MODELPULSE_BROWSER` env → `config.json → browser` →
the Windows default browser (from the registry) → known install paths (Chrome, Edge, Brave,
Vivaldi, Opera, Yandex, Firefox) → the `.html` association. **Chrome is not required.**

### Reports

`data/reports/` gets `report-<timestamp>.html` per run plus `last-run.html` (always the newest);
the 50 latest are kept, older ones are pruned automatically. `data/last-run.txt` holds the same
table as text. Each report is self-contained: inline CSS, no server, opens on a double-click.

### Broken provider links

A provider can look alive in the dashboard and still never answer in chat. Two causes are detected
and shown in a separate panel block:

- **Wrong address.** `baseUrl` holds a model-list URL (`.../v1/models`) or the endpoint itself
  (`.../chat/completions`). The agent appends its own path and gets 404. The monitor repairs the
  address on the fly while probing, which is exactly why such a model looks healthy here.
- **Duplicate provider.** Same root, same key, same models as another entry. It gets zero probes
  (targets are deduplicated) and only doubles the model in the agent's list.

Each row shows the exact wrong address, the models affected, the replacement address and a plain
explanation. **Fix address** / **Delete duplicate** ask for confirmation naming the settings file
and the old → new value, then write the change and keep a backup in `data/backups/`. Nothing is
edited without that confirmation. Console equivalent: the same audit is returned by `GET /api/links`.

### Output token caps

Some providers cap *output tokens per minute* far below the probe budget (Groq free tier: 1000).
Asking for 4096 then fails with `Request too large ... (OTPM): Limit 1000`, while a short chat
message would pass. When the reply names such a cap, the probe **retries at the stated limit** and
the note shows it: `output token cap 1000/min - long chat answers will fail`. So a model that answers
`pong` here but dies on a long answer in chat is reported with its real ceiling, not as healthy.

### Sources: agents on this machine

ModelPulse can find **other agents and clients** installed on the PC and pull models from them.
There is a "Sources" tab in the dashboard and a console command:

```bash
python once.py --list-sources          # agents found on this PC
python once.py --list-sources --deep   # plus a profile walk
python once.py --source zcode          # one pass against the chosen agent
```

**The user picks which agent to work with**: click an agent row in the panel to make it active, and
"Scan once" runs against its models. The choice is saved to `config.json` (`active_source`) and
survives restarts.

Known-agent catalog: Qoder, zcode, Codex CLI, free-claude-code, Cline, Claude Code, Gemini CLI,
Copilot CLI, LM Studio, Ollama, Continue, Aider. Deep scan (`--deep`, or the checkbox in the panel)
also walks the profile and recognises unknown configs by their providers/models shape.

Sources come in three kinds: **agent** (a real client you use; some are probeable), **catalog**
(a model list shipped with an agent), and **registry** (metadata such as `~/.cache/kilo/models.json`
- not your config, shown as reference only and never probed).

**Security.** The inventory **never shows key values** - only a `has_key` flag and its length. Keys
are read into memory only at probe time and never written. The walk is bounded (depth 5, up to
40000 files, a size cap, pruned caches/system dirs) - not a full-disk crawl.

### MCP connector

Add a stdio server under `mcpServers` in `~/.qoder/settings.json`:

```json
{
  "mcpServers": {
    "models-ping": {
      "type": "stdio",
      "command": "python",
      "args": ["<path>/mcp_server.py"]
    }
  }
}
```

Tools: `models_providers` (list targets) and `models_status` (run a pass + summary).

### Privacy

API keys are read from your `settings.json` into memory and are **never written anywhere** —
no reports, no DB, no logs, no disk. The server binds to `127.0.0.1` only.

---

## Русский

**Простыми словами:** ты подключил к агенту кучу своих моделей (свои провайдеры, свои ключи).
ModelPulse показывает, какие из них реально отвечают **сейчас**, а какие лежат — чтобы
переключиться до того, как получишь ошибку.

### Что это

**Одной фразой:** шлёт каждой твоей модели крошечный настоящий запрос и показывает, кто ответил —
чтобы не гадать.

Речь про **твои собственные (кастомные) провайдеры** — те, что ты подключил сам, с ключом. Не про
штатные модели агента: они обычно и так помечены как доступные. А вот твои собственные тихо ложатся,
и никто об этом не сообщает. Именно их ModelPulse и проверяет.

Агент хранит подключённые тобой провайдеры и модели в своём файле настроек — по умолчанию это
`~/.qoder/settings.json` → `providers`.
ModelPulse читает **тот же файл** и активно прозванивает каждую модель запросом
`chat/completions` («ответь одним словом: pong»), проверяя, что пришёл **настоящий текст ответа**,
а не просто HTTP 200. Никакого IPC и никаких плагинов — общий файл-реестр и есть «контракт стыковки».

Статусы:

- **ОТВЕТ** — пришёл текст (`pong`);
- **ПУСТО** — 200, но контента нет (только reasoning / пустой `content`);
- **СБОЙ** — ошибка сети/HTTP (403, 404, 429, таймаут и т.д.).

### Режимы

1. **Разовый прогон** — `once.cmd`. Прозванивает все модели, пишет HTML-отчёт и **открывает его
   отдельным окном браузера**; окно консоли можно закрыть — отчёт остаётся на диске.
2. **Веб-монитор** — `start.cmd` поднимает локальный сервер (`127.0.0.1:8765`) с дашбордом:
   карточки, фильтр, история по каждой модели, доступность за 1ч/24ч. Проверки включают две
   отдельные кнопки: **«Разовый скан»** делает один проход и останавливается, **«Постоянный скан»**
   включает или выключает цикл повторных проверок. Монитор стартует в ожидании — ничего не
   прозванивается, пока не нажмёшь одну из кнопок (поле «интервал» — пауза между проходами в
   постоянном режиме). Идущую проверку можно прервать: **Esc** или кнопка «Отменить». Отменённый
   прогон отбрасывается целиком — в историю и статистику он не попадает; выключение постоянного
   режима даёт текущему проходу доиграть, а не обрывает его.
3. **MCP-тул** — `mcp_server.py` отдаёт инструмент агенту, который по запросу делает тот же прогон.

### Язык интерфейса и тема

Дашборд и отчёт двуязычные: **RU / EN**. Переключатель — сегмент `RU | EN` в правом верхнем углу
панели; выбор сохраняется в `config.json → language` и применяется к следующему отчёту `once.py`.
Разово переопределить: `python once.py --lang en`.

Ещё в панели переключается **тема**: ☀ светлая, ☾ тёмная, 🖥 как в системе. Выбор хранится в
`localStorage` (`mp-theme`) — чтобы примениться сразу при следующей загрузке — и дублируется в
`config.json → theme`, так что свежий браузер подхватит последний выбор. «Как в системе» следит
за ОС на ходу.

### Требования

- Python 3.10+ (проверено на 3.14, Windows и Linux).
- Больше ничего: HTTP — через стандартный `urllib`, хранилище — стандартный `sqlite3`.

### Быстрый старт

```bash
# разовый прогон + отчёт в браузере
python once.py

# только несколько моделей
python once.py --filter wusrouter --timeout 30

# отчёт без открытия окна
python once.py --no-open

# веб-монитор на http://127.0.0.1:8765
python server.py --port 8765 --interval 900
```

На Windows то же самое двойным кликом: `once.cmd` и `start.cmd`.

### Как задать источник и браузер

`config.json` (рядом со скриптами):

```json
{
  "settings_path": "",
  "interval_seconds": 900,
  "max_tokens": 4096,
  "timeout_seconds": 30,
  "workers": 8,
  "language": "ru",
  "theme": "system",
  "browser": ""
}
```

- `settings_path` — путь к файлу настроек; пусто = `~/.qoder/settings.json`.
- `browser` — чем открывать отчёт: полный путь к `.exe` **или** слово
  `firefox` / `chrome` / `edge`. Пусто = браузер по умолчанию Windows.
- `workers` — сколько моделей проверять параллельно.
- `max_tokens` — бюджет ответа на прозвон. Reasoning-модели тратят токены на размышление до
  текста, поэтому маленькое значение делает их «мёртвыми»: держи запас (по умолчанию 4096).

Браузер выбирается по порядку: переменная `MODELPULSE_BROWSER` → `config.json → browser` →
браузер по умолчанию Windows (из реестра) → известные пути (Chrome, Edge, Brave, Vivaldi,
Opera, Yandex, Firefox) → ассоциация `.html`. **Chrome не обязателен.**

### Отчёты

Разовый прогон кладёт отчёты в `data/reports/`:

- `report-<дата_время>.html` — снимок каждого прогона;
- `last-run.html` — всегда последний (удобно держать на закладке);
- хранится 50 последних, старые удаляются автоматически;
- `data/last-run.txt` — та же таблица текстом (как раньше).

Отчёт самодостаточен: весь CSS внутри, сервер не нужен, открывается двойным кликом.

### Битые ссылки провайдеров

Провайдер может выглядеть живым в панели и при этом никогда не отвечать в чате. Две причины
находятся автоматически и показываются отдельным блоком:

- **Не тот адрес.** В `baseUrl` вставлен адрес списка моделей (`.../v1/models`) или самого эндпоинта
  (`.../chat/completions`). Агент добавляет к нему свой путь и получает 404. Монитор при прозвоне
  чинит адрес на лету — именно поэтому такая модель здесь выглядит здоровой.
- **Дубль провайдера.** Тот же корень, тот же ключ, те же модели, что у другой записи. Прозвонов у
  него ноль (цели схлопываются), а в списке агента модель просто двоится.

В каждой строке — точный неверный адрес, затронутые модели, правильный адрес и понятное объяснение.
**«Исправить адрес»** / **«Удалить дубль»** сначала спрашивают подтверждение, где названы файл
настроек и старое → новое значение, и только потом пишут правку; бэкап остаётся в `data/backups/`.
Без подтверждения ничего не меняется. Из консоли тот же аудит отдаёт `GET /api/links`.

### Лимит токенов на ответ

У части провайдеров потолок *выходных токенов в минуту* намного ниже бюджета прозвона (бесплатный
Groq: 1000). Запрос на 4096 тогда падает с `Request too large ... (OTPM): Limit 1000`, хотя короткое
сообщение в чате прошло бы. Если в ответе назван такой потолок, прозвон **повторяется на этом
лимите**, а примечание показывает его: `лимит 1000 токенов/мин на ответ — длинный ответ в чате не
пройдёт`. Модель, которая отвечает здесь `pong`, но умирает на длинном ответе в чате, показывается
со своим реальным потолком, а не как здоровая.

### Источники: агенты на компьютере

ModelPulse умеет искать на ПК **другие агенты и клиенты** и брать модели из них. Есть вкладка
«Источники» в веб-панели и команда в консоли:

```bash
python once.py --list-sources          # список найденных агентов
python once.py --list-sources --deep   # + обход профиля
python once.py --source zcode          # разовый прогон по выбранному агенту
```

**Клиент сам выбирает, с каким агентом работать**: в панели щёлкни строку агента — он станет
активным, и «Разовый скан» пойдёт по его моделям. Выбор сохраняется в `config.json`
(`active_source`) и переживает перезапуск.

Каталог известных агентов: Qoder, zcode, Codex CLI, free-claude-code, Cline, Claude Code,
Gemini CLI, Copilot CLI, LM Studio, Ollama, Continue, Aider. Глубокий поиск (`--deep`, флажок
в панели) дополнительно обходит профиль и распознаёт чужие конфиги по форме провайдеров/моделей.

Источники делятся на три вида:

- **агент** — реальный клиент, ты им пользуешься; у части есть ключ и OpenAI-совместимый адрес,
  такие можно **прозванивать** (например Qoder);
- **каталог** — список моделей рядом с агентом (напр. `codex-model-catalog.json`);
- **реестр** — метаданные вроде `~/.cache/kilo/models.json`, это **не твой конфиг**, только
  справочник имён моделей; помечается явно и никогда не прозванивается.

**Безопасность.** Инвентарь **не показывает значения ключей** — только флаг «есть ключ» и его
длину. Ключи читаются в память лишь в момент прозвона и никуда не пишутся. Обход ограничен
(глубина 5, до 40000 файлов, лимит размера, пропуск кэшей и системных папок) — это не сплошное
сканирование диска.

### MCP-коннектор

В `~/.qoder/settings.json` → `mcpServers` добавь stdio-сервер:

```json
{
  "mcpServers": {
    "models-ping": {
      "type": "stdio",
      "command": "python",
      "args": ["<путь>/mcp_server.py"]
    }
  }
}
```

Инструменты: `models_providers` (список целей) и `models_status` (прогон + сводка).

### О приватности

Ключи читаются из твоего `settings.json` в память и **никуда не пишутся**: ни в отчёты,
ни в БД, ни в логи, ни на диск. Сервер слушает только `127.0.0.1`.

---

## License

MIT — see [LICENSE](LICENSE).
