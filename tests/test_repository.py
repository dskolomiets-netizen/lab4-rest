# Репозиторий отдельно от HTTP: данные переживают перезапуск, id не переиспользуются.

from pathlib import Path

from app.repository import SqliteTrackRepository
from app.schemas import ItemCreate


def _track(title: str) -> ItemCreate:
    return ItemCreate(title=title, artists=["Artist"], duration_sec=200)


def test_data_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "tracks.db"
    before = SqliteTrackRepository(path)
    item = before.add(_track("Numb"))

    # "Перезапуск сервиса": новый объект репозитория поверх того же файла
    after = SqliteTrackRepository(path)

    assert after.get(item.id) == item
    assert after.page(limit=10, offset=0) == ([item], 1)


def test_deleted_id_is_not_given_to_a_new_track(repo: SqliteTrackRepository) -> None:
    first = repo.add(_track("One"))
    assert repo.delete(first.id)

    second = repo.add(_track("Two"))

    assert second.id != first.id
    assert repo.get(first.id) is None


def test_search_treats_percent_and_underscore_literally(repo: SqliteTrackRepository) -> None:
    pure = repo.add(_track("100% Pure Love"))
    repo.add(_track("Numb"))
    snake = repo.add(_track("snake_case"))

    assert repo.search("%", limit=10, offset=0) == ([pure], 1)
    assert repo.search("_", limit=10, offset=0) == ([snake], 1)
