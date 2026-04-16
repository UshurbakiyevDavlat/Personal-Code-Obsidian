# Personal Code Obsidian — Техническое задание

> Рабочий документ. Обновляется по ходу разработки.

---

## 1. Цель

Инструмент, который строит граф зависимостей кодовой базы и отвечает на архитектурные вопросы через MCP в Cowork.

**Проблема:** при работе с большим репо (особенно чужим или давно не трогаемым) непонятно — где что лежит, что на что влияет, что сломается если изменить вот этот сервис.

**Решение:** парсим репо → строим граф (функции, классы, файлы, модули) → через MCP из Cowork можно спросить «покажи зависимости OrderService» или «что сломается если я изменю AuthMiddleware».

**Что НЕ делаем:**
- Не строим граф для чужих/open-source репо — только свои
- Не делаем multimodal (video/audio) — только код + docs
- Не заменяем IDE — дополняем, работаем из Cowork

---

## 2. Стек (принято, не обсуждается)

| Слой | Технология | Почему |
|---|---|---|
| Парсинг | tree-sitter | 25+ языков, AST без LLM, быстро |
| Граф-алгоритмы | NetworkX | Leiden clustering, centrality, paths |
| Хранение | SQLite + FTS5 | Простая персистентность, до ~100K узлов |
| Сервер | FastAPI (Python) | Тесная интеграция с NetworkX, Claude API |
| Визуализация | vis.js | Интерактивный граф в браузере |
| Деплой | Docker + Nginx на VPS | Ubuntu 22.04, простой CI/CD |
| Интеграция | MCP protocol | Подключение в Cowork |

---

## 3. Схема данных

### nodes
```sql
CREATE TABLE nodes (
    id           TEXT PRIMARY KEY,  -- "{repo_id}::{file}::{ClassName}::{method_name}"
    repo_id      TEXT NOT NULL,
    type         TEXT NOT NULL,     -- function | class | file | module
    name         TEXT NOT NULL,     -- "ClassName::methodName" (никогда просто "methodName")
    file_path    TEXT NOT NULL,
    language     TEXT NOT NULL,     -- php | go | typescript | python
    line_start   INTEGER,
    line_end     INTEGER,
    docstring    TEXT,
    metadata     JSON,
    file_hash    TEXT,              -- для инкрементального обновления
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE VIRTUAL TABLE nodes_fts USING fts5(
    name, docstring, file_path,
    content=nodes, content_rowid=rowid
);
```

### edges
```sql
CREATE TABLE edges (
    id           TEXT PRIMARY KEY,
    repo_id      TEXT NOT NULL,
    source_id    TEXT NOT NULL REFERENCES nodes(id),
    target_id    TEXT NOT NULL REFERENCES nodes(id),
    relation     TEXT NOT NULL,     -- calls | imports | inherits | uses | contains
    confidence   TEXT NOT NULL,     -- EXTRACTED | INFERRED | AMBIGUOUS
    weight       REAL DEFAULT 1.0,  -- EXTRACTED=1.0, INFERRED=0.7, AMBIGUOUS=0.4
    source_file  TEXT,
    source_line  INTEGER,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_edges_source ON edges(source_id);
CREATE INDEX idx_edges_target ON edges(target_id);
CREATE INDEX idx_edges_repo   ON edges(repo_id);
```

### repos
```sql
CREATE TABLE repos (
    id           TEXT PRIMARY KEY,  -- slug от пути: "mercuryx-api"
    path         TEXT NOT NULL,
    name         TEXT NOT NULL,
    languages    JSON,              -- ["php", "go"]
    last_indexed TIMESTAMP,
    node_count   INTEGER DEFAULT 0,
    edge_count   INTEGER DEFAULT 0,
    status       TEXT DEFAULT 'pending'  -- pending | indexing | ready | error
);
```

---

## 4. MCP API (инструменты для Cowork)

### graph_build
```
graph_build(repo_path: str, force: bool = False) -> BuildResult
```
Строит или обновляет граф для репо. При `force=True` — полный ребилд, иначе инкрементально по `file_hash`.

### graph_query
```
graph_query(query: str, repo_id: str) -> list[Node]
```
Поиск компонентов по имени или описанию. Сначала FTS5, потом семантический fallback.

### graph_impact
```
graph_impact(component: str, repo_id: str, depth: int = 3) -> ImpactGraph
```
Что сломается при изменении компонента. Ego-граф глубиной `depth` по входящим рёбрам.

### graph_path
```
graph_path(from_component: str, to_component: str, repo_id: str) -> list[Node]
```
Кратчайший путь между двумя компонентами (как они связаны).

### graph_dependencies
```
graph_dependencies(component: str, repo_id: str) -> DependencyTree
```
Входящие (кто зависит от него) и исходящие (от чего зависит он) рёбра.

### graph_overview
```
graph_overview(repo_id: str) -> RepoOverview
```
Сводка по репо: critical nodes (высокий betweenness centrality), top communities, stats.

### graph_list_repos
```
graph_list_repos() -> list[RepoInfo]
```
Список проиндексированных репо с их статусом и датой последней индексации.

---

## 5. Правила парсера (уроки из graphify)

### 5.1 Обязательные exclude-пути
Всегда исключать при индексации (конфигурируется в `.codeobsidian.yml`):

```yaml
exclude:
  - vendor/
  - node_modules/
  - public/js/
  - public/css/
  - storage/
  - bootstrap/cache/
  - .git/
  - tests/         # опционально
  - "**/migrations/**"  # опционально — много дублей up()/down()
```

> **Почему это критично:** graphify на mercuryx-api нашёл 381K нод, из которых 374K (98%) — vendor. Без фильтрации граф бесполезен.

### 5.2 Naming convention для нод
Никогда не сохранять просто `methodName()` — всегда с контекстом:

```
# Плохо (проблема из graphify)
up()
down()
__construct()

# Хорошо
AddAuthViaColumnToLeadsTable::up()
OrderService::__construct()
UserRepository::findById()
```

ID ноды строится как: `{repo_id}::{relative_file_path}::{ClassName}::{method_name}`

### 5.3 Обработка циклических зависимостей
- Использовать `NetworkX.simple_cycles()` при построении графа
- Помечать циклические рёбра флагом `is_cycle=True` в metadata
- Не падать — продолжать индексацию

### 5.4 Confidence scores
| Тип | Score | Когда |
|---|---|---|
| EXTRACTED | 1.0 | Явно в коде: вызов функции, import, наследование |
| INFERRED | 0.7 | Логическая связь: похожие имена, shared data objects |
| AMBIGUOUS | 0.4 | Динамические вызовы, полиморфизм |

### 5.5 Языки (приоритет реализации)
1. **PHP** — основной язык MPS-экосистемы
2. **Go** — микросервисы
3. **TypeScript** — фронт/API клиенты
4. Python, Rust — по необходимости

---

## 6. Конфиг репо (.codeobsidian.yml)

Файл кладётся в корень индексируемого репо:

```yaml
name: mercuryx-api
languages:
  - php
  - go
exclude:
  - vendor/
  - node_modules/
  - public/
  - storage/
include_docs:
  - README.md
  - docs/
```

---

## 7. Фазы реализации

### Фаза 0 — Подготовка ✅
- [x] Создать структуру репо `personal-code-obsidian/`
- [x] Установить и прогнать graphify на реальном репо
- [x] Изучить edge cases из graphify output
- [x] Написать ТЗ

---

### Фаза 1 — Parser Layer (5–7 дней)

**Цель:** на входе — путь к репо, на выходе — заполненные таблицы `nodes` и `edges`.

**Чеклист:**
- [ ] `parser/base.py` — абстрактный класс `BaseParser` с методами `parse_file()`, `extract_nodes()`, `extract_edges()`
- [ ] `parser/php.py` — PHP парсер через tree-sitter
  - [ ] Извлекает: классы, методы, функции, интерфейсы, трейты
  - [ ] Рёбра: calls, imports (use/require), inherits, implements
  - [ ] Node naming: `ClassName::methodName`
- [ ] `parser/go.py` — Go парсер
  - [ ] Извлекает: функции, структуры, интерфейсы, пакеты
  - [ ] Рёбра: calls, imports
- [ ] `parser/typescript.py` — TypeScript парсер
  - [ ] Извлекает: классы, функции, интерфейсы, типы
  - [ ] Рёбра: calls, imports, extends
- [ ] `parser/indexer.py` — оркестратор
  - [ ] Читает `.codeobsidian.yml`
  - [ ] Применяет exclude-правила
  - [ ] Инкрементальное обновление по `file_hash`
  - [ ] Прогресс-бар для CLI
- [ ] `graph/db.py` — SQLite + FTS5 инициализация и CRUD
- [ ] `graph/storage.py` — сохранение нод и рёбер батчами
- [ ] `tests/test_parser_php.py` — тесты на реальных PHP сниппетах
- [ ] `tests/test_parser_go.py`

**Definition of Done фазы 1:**
- `python -m parser.indexer /path/to/repo` отрабатывает без ошибок
- Граф для среднего репо строится < 60 сек
- Vendor/node_modules отфильтрованы
- Node naming правильный (`Class::method`, не просто `method`)

---

### Фаза 2 — Query Engine (3–4 дня)

**Цель:** умные запросы поверх SQLite + NetworkX.

**Чеклист:**
- [ ] `graph/loader.py` — загружает SQLite граф в NetworkX для алгоритмов
- [ ] `graph/queries.py` — базовые запросы:
  - [ ] `search_component(name, repo_id)` → FTS5 поиск
  - [ ] `list_dependencies(node_id)` → исходящие рёбра
  - [ ] `list_dependents(node_id)` → входящие рёбра
  - [ ] `find_by_file(file_path, repo_id)` → все ноды файла
- [ ] `graph/algorithms.py` — граф-алгоритмы:
  - [ ] `find_path(from_id, to_id)` → shortest_path
  - [ ] `analyze_impact(node_id, depth)` → ego_graph
  - [ ] `find_cycles(repo_id)` → simple_cycles
  - [ ] `get_critical_nodes(repo_id)` → betweenness centrality top-20
  - [ ] `get_communities(repo_id)` → Leiden clustering
- [ ] `tests/test_queries.py`
- [ ] `tests/test_algorithms.py`

**Definition of Done фазы 2:**
- Поиск компонента по имени < 100ms
- `analyze_impact` для depth=3 < 500ms
- Циклы детектируются и помечаются

---

### Фаза 3 — MCP Server (3–4 дня)

**Цель:** FastAPI сервер, доступный из Cowork через MCP protocol.

**Чеклист:**
- [ ] `server/main.py` — FastAPI app
- [ ] `server/mcp.py` — MCP protocol handler
- [ ] `server/tools/` — по одному файлу на каждый MCP инструмент:
  - [ ] `graph_build.py`
  - [ ] `graph_query.py`
  - [ ] `graph_impact.py`
  - [ ] `graph_path.py`
  - [ ] `graph_dependencies.py`
  - [ ] `graph_overview.py`
  - [ ] `graph_list_repos.py`
- [ ] `server/auth.py` — Bearer token аутентификация
- [ ] `GET /health` — healthcheck endpoint
- [ ] `tests/test_server.py` — smoke тесты через httpx

**Definition of Done фазы 3:**
- Все 7 инструментов отвечают корректно
- Auth работает (неверный токен → 401)
- `/health` возвращает 200

---

### Фаза 4 — VPS Deploy (2–3 дня)

**Цель:** сервер работает на VPS, доступен по HTTPS.

**Чеклист:**
- [ ] `Dockerfile` для API
- [ ] `web/Dockerfile` для vis.js фронта
- [ ] `docker-compose.yml` финальный (volumes, env, healthcheck)
- [ ] `scripts/deploy.sh` — git pull + docker-compose up --build -d
- [ ] `scripts/update-repo.sh` — запуск `graph_build` для конкретного репо
- [ ] Nginx конфиг с SSL (certbot)
- [ ] GitHub Actions: push в main → SSH → deploy
- [ ] `.env.example` заполнен всеми нужными переменными

**Definition of Done фазы 4:**
- `https://YOUR_DOMAIN/health` → 200
- `https://YOUR_DOMAIN/` → vis.js граф открывается
- После push в main деплой происходит автоматически

---

### Фаза 5 — Cowork Integration (1–2 дня)

**Цель:** MCP доступен из Cowork, есть Cowork плагин.

**Чеклист:**
- [ ] Добавить MCP в настройки Cowork:
  ```json
  {
    "name": "code-obsidian",
    "url": "https://YOUR_DOMAIN/mcp",
    "auth": "Bearer YOUR_TOKEN"
  }
  ```
- [ ] Создать Cowork плагин (`create-cowork-plugin` skill) со скилами:
  - [ ] `code-graph:query` — найти компонент
  - [ ] `code-graph:impact` — анализ влияния
  - [ ] `code-graph:overview` — обзор репо
- [ ] Обновить `mps-architecture` skill — использовать `graph_overview` при архитектурных вопросах
- [ ] Протестировать из Cowork: спросить про зависимости реального компонента

**Definition of Done фазы 5:**
- Из Cowork можно спросить: «что зависит от OrderService?» — получить ответ из графа
- Плагин установлен и работает

---

### Фаза 6 — Enhancements (ongoing)

Приоритизируем по необходимости:

- [ ] Семантический поиск через embeddings (docstrings → vector search)
- [ ] GitHub webhook → автоматический ребилд при push
- [ ] Git history analysis — как архитектура менялась во времени
- [ ] Diff граф — что изменилось между двумя коммитами
- [ ] «God Object» детектор — компоненты с аномально высокой централностью
- [ ] Auto-push архитектурного саммари в KB при `graph_build`

---

## 8. Definition of Done (MVP)

Проект считается готовым к использованию когда:

- [ ] Граф строится < 30 сек для среднего репо (< 50K строк кода)
- [ ] Поиск компонента < 1 сек
- [ ] Визуализация работает smooth для 500+ нод
- [ ] Vendor и сгенерированные файлы отфильтрованы
- [ ] Node naming однозначный (`Class::method`)
- [ ] Все 7 MCP инструментов работают из Cowork
- [ ] Деплой автоматический через GitHub Actions
- [ ] Есть `.codeobsidian.yml` конфиг для настройки под конкретное репо

---

## 9. Структура репо

```
personal-code-obsidian/
├── parser/
│   ├── __init__.py
│   ├── base.py          ← абстрактный парсер
│   ├── php.py
│   ├── go.py
│   ├── typescript.py
│   └── indexer.py       ← оркестратор
├── graph/
│   ├── __init__.py
│   ├── db.py            ← SQLite + FTS5
│   ├── storage.py       ← CRUD
│   ├── loader.py        ← SQLite → NetworkX
│   ├── queries.py       ← базовые запросы
│   └── algorithms.py    ← NetworkX алгоритмы
├── server/
│   ├── __init__.py
│   ├── main.py          ← FastAPI app
│   ├── mcp.py           ← MCP protocol
│   ├── auth.py
│   └── tools/
│       ├── graph_build.py
│       ├── graph_query.py
│       ├── graph_impact.py
│       ├── graph_path.py
│       ├── graph_dependencies.py
│       ├── graph_overview.py
│       └── graph_list_repos.py
├── web/
│   ├── Dockerfile
│   └── index.html       ← vis.js визуализация
├── scripts/
│   ├── deploy.sh
│   └── update-repo.sh
├── tests/
│   ├── test_parser_php.py
│   ├── test_parser_go.py
│   ├── test_queries.py
│   ├── test_algorithms.py
│   └── test_server.py
├── docker-compose.yml
├── Dockerfile
├── .env.example
├── .gitignore
├── SPEC.md              ← этот файл
└── README.md
```
