# Контракт API: модель входа ItemCreate и модели выхода ItemRead / ItemPage.
# Ресурс - трек музыкальной библиотеки (та же сущность, что Track в ЛР3).
# Ограничения полей проверяет Pydantic: нарушение -> 422 без единого if в обработчиках.
# Docstring-и классов попадают в OpenAPI (/docs), поэтому они на английском.

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# Имя исполнителя: после обрезки пробелов от 1 до 100 символов
ArtistName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]

# ID трека в Spotify: ровно 22 символа base62 (как в ЛР3)
SPOTIFY_ID_PATTERN = r"^[0-9A-Za-z]{22}$"


class ItemCreate(BaseModel):
    """Track sent by the client: every field except id and timestamps."""

    # extra="forbid": лишнее поле (например, id) в теле -> 422, а не молчаливый пропуск;
    # str_strip_whitespace: "   " обрезается до "" и не проходит min_length=1
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        json_schema_extra={
            "examples": [
                {
                    "title": "Numb",
                    "artists": ["Linkin Park"],
                    "album": "Meteora",
                    "duration_sec": 187,
                    "year": 2003,
                    "spotify_id": "2nLtzopw4rPReszdYBJU6h",
                }
            ]
        },
    )

    title: str = Field(min_length=1, max_length=200, description="track title")
    artists: list[ArtistName] = Field(min_length=1, max_length=10, description="1..10 artists")
    album: str = Field(default="", max_length=200, description="album title, may be empty")
    duration_sec: int = Field(ge=1, le=24 * 3600, description="duration in seconds")
    year: int | None = Field(default=None, ge=1900, le=2100, description="release year")
    spotify_id: str | None = Field(
        default=None,
        pattern=SPOTIFY_ID_PATTERN,
        description="22-char Spotify track id, unique in the library",
    )


class ItemRead(ItemCreate):
    """Track as stored by the service: input fields plus id and timestamps."""

    # Ответ сервера клиент читает терпимо: новое поле на сервере не сломает старый клиент
    model_config = ConfigDict(extra="ignore")

    id: int
    created_at: datetime
    updated_at: datetime


class ItemPage(BaseModel):
    """One page of tracks plus the total number of matches."""

    items: list[ItemRead]
    total: int = Field(description="matches for the whole query, not just this page")
    limit: int
    offset: int


class ErrorMessage(BaseModel):
    """Error body for 404 and 409 responses."""

    detail: str
