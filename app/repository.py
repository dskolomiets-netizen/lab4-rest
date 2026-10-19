# Слой хранения (репозиторий): обработчики API не знают ни про SQL, ни про файл БД.
# TrackRepository - контракт (Protocol, как в ЛР3), SqliteTrackRepository - реализация
# на sqlite3 из стандартной библиотеки. Данные лежат в файле и переживают перезапуск.

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from app.schemas import ItemCreate, ItemRead


class DuplicateTrackError(Exception):
    # Трек с таким spotify_id уже есть: API превращает это в 409 Conflict
    def __init__(self, spotify_id: str) -> None:
        super().__init__(f"track with spotify_id {spotify_id} already exists")
        self.spotify_id = spotify_id


class TrackRepository(Protocol):
    def add(self, data: ItemCreate) -> ItemRead: ...

    def get(self, item_id: int) -> ItemRead | None: ...

    # Страница треков и общее число треков (для пагинации на клиенте)
    def page(self, limit: int, offset: int) -> tuple[list[ItemRead], int]: ...

    def search(self, query: str, limit: int, offset: int) -> tuple[list[ItemRead], int]: ...

    # Полная замена полей (семантика PUT); None - трека нет
    def replace(self, item_id: int, data: ItemCreate) -> ItemRead | None: ...

    # True - удалили, False - трека не было
    def delete(self, item_id: int) -> bool: ...


# AUTOINCREMENT: id удалённого трека не выдаётся повторно, поэтому старая ссылка
# /items/3 после DELETE навсегда даёт 404, а не чужой трек.
# title_fold - название в casefold() для поиска без учёта регистра: встроенные
# LIKE / lower() в SQLite понимают регистр только у латиницы, а у нас "Кино".
# spotify_id UNIQUE: повторный импорт того же трека даёт 409, а не дубль (NULL не мешает).
_SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT    NOT NULL,
    title_fold   TEXT    NOT NULL,
    artists      TEXT    NOT NULL,
    album        TEXT    NOT NULL,
    duration_sec INTEGER NOT NULL,
    year         INTEGER,
    spotify_id   TEXT    UNIQUE,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL
)
"""

_COLUMNS = "id, title, artists, album, duration_sec, year, spotify_id, created_at, updated_at"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _to_item(row: sqlite3.Row) -> ItemRead:
    return ItemRead(
        id=row["id"],
        title=row["title"],
        artists=json.loads(row["artists"]),
        album=row["album"],
        duration_sec=row["duration_sec"],
        year=row["year"],
        spotify_id=row["spotify_id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _fields(data: ItemCreate) -> tuple[str, str, str, str, int, int | None, str | None]:
    # Порядок совпадает с колонками title, title_fold, artists, album, duration_sec, year, spotify_id
    return (
        data.title,
        data.title.casefold(),
        # ensure_ascii=False: в файле БД кириллица хранится как есть, а не \uXXXX
        json.dumps(data.artists, ensure_ascii=False),
        data.album,
        data.duration_sec,
        data.year,
        data.spotify_id,
    )


class SqliteTrackRepository:
    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(_SCHEMA)

    @property
    def path(self) -> Path:
        return self._path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # Новое соединение на каждую операцию: обычные (def) обработчики FastAPI
        # выполняет в пуле потоков, а одно соединение sqlite3 нельзя делить между потоками
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:  # commit при успехе, rollback при исключении
                yield conn
        finally:
            conn.close()

    def add(self, data: ItemCreate) -> ItemRead:
        now = _now()
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    "INSERT INTO tracks (title, title_fold, artists, album, duration_sec, year,"
                    " spotify_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (*_fields(data), now, now),
                )
                row = conn.execute(
                    f"SELECT {_COLUMNS} FROM tracks WHERE id = ?", (cursor.lastrowid,)
                ).fetchone()
        except sqlite3.IntegrityError as exc:
            raise DuplicateTrackError(data.spotify_id or "") from exc
        return _to_item(row)

    def get(self, item_id: int) -> ItemRead | None:
        with self._connect() as conn:
            row = conn.execute(f"SELECT {_COLUMNS} FROM tracks WHERE id = ?", (item_id,)).fetchone()
        return _to_item(row) if row else None

    def page(self, limit: int, offset: int) -> tuple[list[ItemRead], int]:
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM tracks ORDER BY id LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        return [_to_item(row) for row in rows], int(total)

    def search(self, query: str, limit: int, offset: int) -> tuple[list[ItemRead], int]:
        # instr() ищет подстроку буквально: "%" и "_" в запросе - обычные символы,
        # а не шаблоны, как было бы с LIKE
        needle = query.casefold()
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM tracks WHERE instr(title_fold, ?) > 0"
                " ORDER BY id LIMIT ? OFFSET ?",
                (needle, limit, offset),
            ).fetchall()
            total = conn.execute(
                "SELECT COUNT(*) FROM tracks WHERE instr(title_fold, ?) > 0", (needle,)
            ).fetchone()[0]
        return [_to_item(row) for row in rows], int(total)

    def replace(self, item_id: int, data: ItemCreate) -> ItemRead | None:
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    "UPDATE tracks SET title = ?, title_fold = ?, artists = ?, album = ?,"
                    " duration_sec = ?, year = ?, spotify_id = ?, updated_at = ? WHERE id = ?",
                    (*_fields(data), _now(), item_id),
                )
                if cursor.rowcount == 0:
                    return None
                row = conn.execute(
                    f"SELECT {_COLUMNS} FROM tracks WHERE id = ?", (item_id,)
                ).fetchone()
        except sqlite3.IntegrityError as exc:
            raise DuplicateTrackError(data.spotify_id or "") from exc
        return _to_item(row)

    def delete(self, item_id: int) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM tracks WHERE id = ?", (item_id,))
        return cursor.rowcount > 0
