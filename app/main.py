# REST-сервис музыкальной библиотеки: CRUD над треками, список с пагинацией, поиск.
# Запуск: uvicorn app.main:app --reload  ->  документация на http://127.0.0.1:8000/docs
# Docstring-и обработчиков FastAPI показывает в /docs, поэтому они на английском.

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse

from app.repository import DuplicateTrackError, SqliteTrackRepository, TrackRepository
from app.schemas import ErrorMessage, ItemCreate, ItemPage, ItemRead

# Рабочая БД; путь можно переопределить переменной окружения TRACKS_DB
DEFAULT_DB = Path("data") / "tracks.db"


class AsciiJSONResponse(JSONResponse):
    # Весь вывод программы - ASCII: кириллица уходит в JSON как \uXXXX.
    # Это валидный JSON, и клиент после разбора получает исходные строки без потерь.
    # Явный класс ответа заодно отключает быстрый путь FastAPI (Pydantic сразу в UTF-8).
    def render(self, content: Any) -> bytes:
        return json.dumps(content, ensure_ascii=True, allow_nan=False).encode("ascii")


@lru_cache(maxsize=1)
def get_repository() -> TrackRepository:
    # Рабочая БД открывается лениво, при первом запросе. Тесты подменяют эту
    # зависимость (app.dependency_overrides) и до рабочего файла не доходят.
    return SqliteTrackRepository(Path(os.environ.get("TRACKS_DB", DEFAULT_DB)))


Repo = Annotated[TrackRepository, Depends(get_repository)]
Limit = Annotated[int, Query(ge=1, le=100, description="page size, 1..100")]
Offset = Annotated[int, Query(ge=0, description="how many tracks to skip")]

NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorMessage, "description": "Track not found"}
}
CONFLICT: dict[int | str, dict[str, Any]] = {
    409: {"model": ErrorMessage, "description": "spotify_id already used by another track"}
}

app = FastAPI(
    title="Music Library API",
    version="1.0.0",
    summary="Lab 4: REST service over music tracks (FastAPI + Pydantic + SQLite).",
    default_response_class=AsciiJSONResponse,
)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError) -> AsciiJSONResponse:
    # То же тело 422, что у FastAPI по умолчанию, но тоже в ASCII
    # (в ошибке может быть эхо ввода клиента, например кириллица)
    return AsciiJSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={"detail": jsonable_encoder(exc.errors())},
    )


def _not_found(item_id: int) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"track {item_id} not found")


def _conflict(exc: DuplicateTrackError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/docs")


@app.post(
    "/items",
    response_model=ItemRead,
    status_code=status.HTTP_201_CREATED,
    responses=CONFLICT,
    tags=["items"],
)
def create_item(data: ItemCreate, repo: Repo, response: Response) -> ItemRead:
    """Add a track. Returns 201 with the stored track, its new id and a Location header."""
    try:
        item = repo.add(data)
    except DuplicateTrackError as exc:
        raise _conflict(exc) from exc
    response.headers["Location"] = f"/items/{item.id}"
    return item


@app.get("/items", response_model=ItemPage, tags=["items"])
def list_items(repo: Repo, limit: Limit = 20, offset: Offset = 0) -> ItemPage:
    """List tracks ordered by id, one page at a time."""
    items, total = repo.page(limit, offset)
    return ItemPage(items=items, total=total, limit=limit, offset=offset)


# Порядок важен: /items/search объявлен раньше /items/{item_id}. Иначе слово
# "search" попало бы в item_id и запрос получил бы 422 "не целое число".
@app.get("/items/search", response_model=ItemPage, tags=["items"])
def search_items(
    repo: Repo,
    q: Annotated[str, Query(min_length=1, max_length=100, description="part of the title")],
    limit: Limit = 20,
    offset: Offset = 0,
) -> ItemPage:
    """Find tracks whose title contains q (case-insensitive, Cyrillic too)."""
    items, total = repo.search(q, limit, offset)
    return ItemPage(items=items, total=total, limit=limit, offset=offset)


@app.get("/items/{item_id}", response_model=ItemRead, responses=NOT_FOUND, tags=["items"])
def get_item(item_id: int, repo: Repo) -> ItemRead:
    """Get one track by id."""
    item = repo.get(item_id)
    if item is None:
        raise _not_found(item_id)
    return item


@app.put(
    "/items/{item_id}",
    response_model=ItemRead,
    responses={**NOT_FOUND, **CONFLICT},
    tags=["items"],
)
def replace_item(item_id: int, data: ItemCreate, repo: Repo) -> ItemRead:
    """Replace all fields of a track (idempotent: repeating the call changes nothing)."""
    try:
        item = repo.replace(item_id, data)
    except DuplicateTrackError as exc:
        raise _conflict(exc) from exc
    if item is None:
        raise _not_found(item_id)
    return item


@app.delete(
    "/items/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses=NOT_FOUND,
    tags=["items"],
)
def delete_item(item_id: int, repo: Repo) -> None:
    """Delete a track. Returns 204 with an empty body."""
    if not repo.delete(item_id):
        raise _not_found(item_id)
