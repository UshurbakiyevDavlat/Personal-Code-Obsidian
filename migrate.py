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
    repo_id     TEXT NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    metric      TEXT NOT NULL,
    value_json  TEXT NOT NULL,
    computed_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (repo_id, metric)
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

        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='repo_metrics'"
        )
        if cursor.fetchone():
            print("✅ Проверка: repo_metrics существует в БД")
        else:
            print("❌ Ошибка: таблица не создалась")
    finally:
        conn.close()


if __name__ == "__main__":
    migrate()
