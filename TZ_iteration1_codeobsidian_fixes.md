═══════════════════════════════════════════
ТЗ: Personal-Code-Obsidian — Migration Fix + Docstring Extraction
Проект: Personal-Code-Obsidian · Python 3.12 + SQLite + tree-sitter
Дата: 2026-05-07
═══════════════════════════════════════════

## Цель
1. Добавить недостающую таблицу `repo_metrics` в production SQLite БД — без неё `graph_overview` возвращает пустые данные
2. Включить извлечение docstrings при парсинге — поле всегда None, теряем огромный сигнал для FTS

## Контекст
В production БД `data/graph.db` таблицы `repo_metrics` нет — она была добавлена в schema после создания БД. При вызове `graph_overview` считывается из `repo_metrics`, возвращает None. Отдельно: в `parser/extract.py` поле docstring везде None — парсер его не извлекает. Это убивает качество FTS поиска (`graph_query`) по "найди функцию которая делает X".

## Файлы

**Трогать:**
- `parser/extract.py` — добавить извлечение docstrings для Python, Go, PHP, Rust
- `database/schema.py` или `database/db.py` — убедиться что CREATE TABLE IF NOT EXISTS repo_metrics есть при инициализации

**Создать:**
- `migrate.py` — одноразовый скрипт миграции БД (запустить вручную)

**Не трогать:**
- `graph/algorithms.py` — алгоритмы не меняем
- `agent_server/server.py` (MCP tools) — не меняем интерфейс
- `data/graph.db` — не трогать напрямую, только через migrate.py

## Реализация

### 1. migrate.py — создать скрипт миграции

Создать файл `migrate.py` в корне проекта:

```python
"""
Миграция БД: добавляет отсутствующую таблицу repo_metrics.
Запуск: python migrate.py
Безопасно запускать многократно (CREATE TABLE IF NOT EXISTS).
"""
import sqlite3
from pathlib import Path

DB_PATH = Path("data/graph.db")

MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS repo_metrics (
    repo_id         TEXT PRIMARY KEY,
    computed_at     TEXT NOT NULL,
    node_count      INTEGER DEFAULT 0,
    edge_count      INTEGER DEFAULT 0,
    avg_degree      REAL DEFAULT 0,
    density         REAL DEFAULT 0,
    critical_nodes  TEXT,   -- JSON array
    communities     TEXT,   -- JSON array
    cycles          TEXT,   -- JSON array
    entry_points    TEXT,   -- JSON array
    god_objects     TEXT    -- JSON array
);
"""

def migrate():
    if not DB_PATH.exists():
        print(f"❌ БД не найдена: {DB_PATH}")
        return
    
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.executescript(MIGRATION_SQL)
        conn.commit()
        print("✅ Миграция выполнена: таблица repo_metrics создана (или уже существовала)")
        
        # Проверка
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='repo_metrics'")
        if cursor.fetchone():
            print("✅ Проверка: repo_metrics существует в БД")
        else:
            print("❌ Ошибка: таблица не создалась")
    finally:
        conn.close()

if __name__ == "__main__":
    migrate()
```

Инструкция: после изменений выполнить `python migrate.py` на сервере.

### 2. parser/extract.py — добавить docstring extraction

Найти все места где возвращается `docstring=None` или `docstring=""` и заменить на вызов вспомогательной функции.

Добавить в начало файла функцию `_extract_docstring`:

```python
def _extract_docstring(node, source_bytes: bytes) -> str | None:
    """
    Извлечь docstring из AST-узла функции/класса.
    Поддерживает Python, Go, PHP, Rust.
    """
    # Python: первый child типа expression_statement → string
    for child in node.children:
        if child.type == "block":
            for stmt in child.children:
                if stmt.type == "expression_statement":
                    for expr in stmt.children:
                        if expr.type == "string":
                            raw = source_bytes[expr.start_byte:expr.end_byte].decode("utf-8", errors="replace").strip()
                            return _unquote_string(raw)
                    break  # only inspect the first expression_statement
            break  # only inspect the first block
        elif child.type in ("function_definition", "class_definition"):
            # ВАЖНО: decorated_definition (e.g. @mcp.tool) оборачивает function_definition.
            # child.type == "block" не найти напрямую — нужно рекурсивно зайти в function_definition.
            return _extract_docstring(child, source_bytes)

    # Go: block_comment или line_comment ДО узла
    # Ищем sibling comment перед текущим узлом
    parent = node.parent
    if parent:
        prev = None
        for child in parent.children:
            if child == node:
                if prev and prev.type in ("comment", "block_comment", "line_comment"):
                    comment = source_bytes[prev.start_byte:prev.end_byte].decode("utf-8", errors="replace")
                    return comment.lstrip("//").lstrip("/*").rstrip("*/").strip()
                break
            prev = child

    # Rust: doc-comment /// или //!
    parent = node.parent
    if parent:
        prev = None
        for child in parent.children:
            if child == node:
                if prev and prev.type in ("line_comment", "block_comment"):
                    comment = source_bytes[prev.start_byte:prev.end_byte].decode("utf-8", errors="replace")
                    if comment.startswith("///") or comment.startswith("//!"):
                        return comment.lstrip("///").lstrip("//!").strip()
                break
            prev = child

    # PHP: /** docblock */ — block_comment перед узлом
    parent = node.parent
    if parent:
        prev = None
        for child in parent.children:
            if child == node:
                if prev and prev.type == "comment":
                    comment = source_bytes[prev.start_byte:prev.end_byte].decode("utf-8", errors="replace")
                    if comment.startswith("/**"):
                        # убрать /** и */ и * в начале строк
                        lines = comment.lstrip("/**").rstrip("*/").split("\n")
                        cleaned = " ".join(line.strip().lstrip("*").strip() for line in lines if line.strip().lstrip("*").strip())
                        return cleaned or None
                break
            prev = child

    return None
```

Затем найти все места в extract.py где создаётся объект Node/Function/Class с `docstring=None` или не заполненным docstring, и заменить на:

```python
docstring=_extract_docstring(node, source_bytes) or None
```

**Важно**: `source_bytes` — это `code.encode("utf-8")` от исходного файла. Убедиться что он доступен в scope функции-парсера.

### 3. database/ — убедиться что CREATE TABLE IF NOT EXISTS repo_metrics есть при init

Найти функцию инициализации БД (обычно `init_db()` или аналог в database/db.py или database/schema.py). Добавить туда `MIGRATION_SQL` из пункта 1 — тогда при следующем старте приложения таблица создастся автоматически. Это безопасно: `CREATE TABLE IF NOT EXISTS` идемпотентен.

## Стандарты
- **Karpathy**: Simple — migrate.py одноразовый скрипт, не усложняем приложение. Surgical — только extract.py для docstrings, только migration для schema.
- **Dev**: `dev-standards:python-api` — идемпотентные операции, no-op если уже применено.
- **Проект**: SQLite через прямой sqlite3 (не ORM), tree-sitter node API.

## Что НЕ делать
- **НЕ пересоздавать БД** — только ADD TABLE, данные в nodes/edges не трогать
- **НЕ падать** если docstring не найден — возвращать None, не Exception
- **НЕ делать docstring обязательным полем** — это информационное поле
- **НЕ переиндексировать репозитории принудительно** — docstrings заполнятся при следующем инкрементальном обходе изменённых файлов

## Критерий готовности
- [ ] `python migrate.py` — выполняется без ошибок, выводит "✅ Проверка: repo_metrics существует"
- [ ] `graph_overview(repo_id="AdashAI")` — возвращает communities, critical_nodes (не пустые)
- [ ] После `graph_build` для любого файла с docstring — `graph_query` по словам из docstring находит функцию
- [ ] Для Python: `"""docstring"""` извлекается корректно
- [ ] Для Go/Rust: `// comment` перед функцией извлекается корректно
- [ ] Для файлов без docstring — поле None, не пустая строка
- [ ] Karpathy review: код thoughtful, simple, surgical, goal-driven
