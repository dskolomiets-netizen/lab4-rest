# Общие фикстуры. Каждый тест получает свою пустую БД во временной папке pytest;
# рабочая data/tracks.db не открывается вовсе: зависимость get_repository подменена.

import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient

from app.main import app, get_repository
from app.repository import SqliteTrackRepository


@pytest.fixture
def repo(tmp_path: Path) -> SqliteTrackRepository:
    return SqliteTrackRepository(tmp_path / "tracks.db")


@pytest.fixture
def api(repo: SqliteTrackRepository) -> Iterator[TestClient]:
    app.dependency_overrides[get_repository] = lambda: repo
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def live_url(repo: SqliteTrackRepository) -> Iterator[str]:
    # Настоящий uvicorn на свободном порту в фоновом потоке: клиент ходит по сети,
    # как в работе, только база временная
    app.dependency_overrides[get_repository] = lambda: repo
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)
    sock.close()
    app.dependency_overrides.clear()


@pytest.fixture
def numb() -> dict[str, Any]:
    return {
        "title": "Numb",
        "artists": ["Linkin Park"],
        "album": "Meteora",
        "duration_sec": 187,
        "year": 2003,
        "spotify_id": "2nLtzopw4rPReszdYBJU6h",
    }


@pytest.fixture
def kino() -> dict[str, Any]:
    # Кино - Группа крови: кириллица в названии, исполнителе и альбоме
    return {
        "title": "Группа крови",
        "artists": ["Кино"],
        "album": "Группа крови",
        "duration_sec": 284,
        "year": 1988,
    }
