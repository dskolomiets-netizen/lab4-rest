# Клиент на httpx: настоящий HTTP до uvicorn (live_url), а сбои - через подменённый
# транспорт httpx.MockTransport (сеть не нужна, повторы считаются точно).

import socket
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.schemas import ItemCreate, ItemRead
from client.api import (
    ApiError,
    InvalidInputError,
    LibraryClient,
    NotFoundError,
    ServerError,
    ServiceUnavailableError,
)
from client.cli import main

LAB3_JSON = Path(__file__).resolve().parent.parent / "examples" / "tracks.json"


# --- настоящий HTTP --------------------------------------------------------------


def test_client_covers_every_operation_with_typed_results(
    live_url: str, numb: dict[str, Any]
) -> None:
    with LibraryClient(live_url) as client:
        created = client.create(ItemCreate(**numb))
        assert isinstance(created, ItemRead)
        assert created.id == 1
        assert client.get(created.id) == created
        assert client.list_items().items == [created]
        assert client.search("NUMB").items == [created]

        updated = client.replace(created.id, ItemCreate(**{**numb, "year": 2004}))
        assert updated.year == 2004
        assert updated.created_at == created.created_at

        client.delete(created.id)
        with pytest.raises(NotFoundError, match=r"^not found \(404\): track 1 not found$"):
            client.get(created.id)


def test_client_reports_server_side_422_by_field(live_url: str) -> None:
    # model_construct пропускает локальную проверку: так до сервера доходит плохое тело
    bad = ItemCreate.model_construct(
        title="", artists=["X"], album="", duration_sec=-5, year=None, spotify_id=None
    )

    with LibraryClient(live_url) as client, pytest.raises(InvalidInputError) as info:
        client.create(bad)

    message = str(info.value)
    assert message.startswith("invalid input (422): ")
    assert "title: String should have at least 1 character" in message
    assert "duration_sec: Input should be greater than or equal to 1" in message


def test_timeout_is_set_and_reported() -> None:
    # Сервер, который принимает соединение и молчит: без таймаута клиент ждал бы вечно
    with socket.socket() as silent:
        silent.bind(("127.0.0.1", 0))
        silent.listen()
        url = f"http://127.0.0.1:{silent.getsockname()[1]}"
        with LibraryClient(url, timeout=0.3, retries=0) as client:
            with pytest.raises(ServiceUnavailableError, match=r"did not answer within 0\.3 s"):
                client.get(1)


# --- командная строка ------------------------------------------------------------


def test_cli_imports_lab3_json_without_duplicates(
    live_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--url", live_url, "import", str(LAB3_JSON)]) == 0
    first = capsys.readouterr().out
    assert main(["--url", live_url, "import", str(LAB3_JSON)]) == 0
    second = capsys.readouterr().out
    # "группа" строчными, в файле "Группа крови"
    assert main(["--url", live_url, "search", "группа"]) == 0
    found = capsys.readouterr().out

    assert "imported 4 tracks, skipped 0" in first
    assert "imported 0 tracks, skipped 4" in second
    assert "Kino - Gruppa krovi [Gruppa krovi] 4:44" in found
    assert (first + second + found).isascii()


def test_cli_error_is_one_ascii_line_without_traceback(
    live_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["--url", live_url, "get", "42"])

    assert code == 1
    assert capsys.readouterr().err == "error: not found (404): track 42 not found\n"


def test_cli_checks_input_before_sending(capsys: pytest.CaptureFixture[str]) -> None:
    # Сервера по этому адресу нет: ошибка находится ещё до запроса
    args = ["--url", "http://127.0.0.1:9", "add", "--title", " ", "--artist", "X", "--duration", "3:07"]

    assert main(args) == 1
    assert capsys.readouterr().err == (
        "error: invalid input: title: String should have at least 1 character\n"
    )


# --- сбои через MockTransport ----------------------------------------------------

Handler = Callable[[httpx.Request], httpx.Response]


def _stored() -> dict[str, Any]:
    now = datetime(2026, 10, 18, tzinfo=UTC)
    item = ItemRead(id=1, title="Numb", artists=["Linkin Park"], duration_sec=187,
                    created_at=now, updated_at=now)
    return item.model_dump(mode="json")


@pytest.fixture
def calls() -> list[str]:
    return []


@pytest.fixture
def make_client(calls: list[str]) -> Iterator[Callable[[Handler], LibraryClient]]:
    clients: list[LibraryClient] = []

    def make(handler: Handler) -> LibraryClient:
        def counted(request: httpx.Request) -> httpx.Response:
            calls.append(request.method)
            return handler(request)

        # backoff=0: тест проверяет число попыток, а не ждёт паузы
        client = LibraryClient("http://library.test", backoff=0, transport=httpx.MockTransport(counted))
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.close()


def test_get_is_retried_on_5xx_until_success(
    make_client: Callable[[Handler], LibraryClient], calls: list[str]
) -> None:
    def flaky(request: httpx.Request) -> httpx.Response:
        if len(calls) < 3:
            return httpx.Response(503, json={"detail": "busy"})
        return httpx.Response(200, json=_stored())

    assert make_client(flaky).get(1).title == "Numb"
    assert calls == ["GET", "GET", "GET"]


def test_post_is_never_retried(
    make_client: Callable[[Handler], LibraryClient], calls: list[str]
) -> None:
    client = make_client(lambda request: httpx.Response(503, json={"detail": "busy"}))

    with pytest.raises(ServerError, match=r"server error \(503\): busy"):
        client.create(ItemCreate(title="Numb", artists=["Linkin Park"], duration_sec=187))
    assert calls == ["POST"]


def test_4xx_is_not_retried(
    make_client: Callable[[Handler], LibraryClient], calls: list[str]
) -> None:
    client = make_client(lambda request: httpx.Response(404, json={"detail": "track 5 not found"}))

    with pytest.raises(NotFoundError):
        client.delete(5)
    assert calls == ["DELETE"]


def test_network_failure_after_all_retries_is_readable(
    make_client: Callable[[Handler], LibraryClient], calls: list[str]
) -> None:
    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[WinError 10061] connection refused", request=request)

    with pytest.raises(ServiceUnavailableError) as info:
        make_client(refused).list_items()

    assert str(info.value) == (
        "cannot connect to http://library.test (ConnectError); is the server running?"
    )
    assert calls == ["GET", "GET", "GET"]


def test_unexpected_body_is_an_api_error(make_client: Callable[[Handler], LibraryClient]) -> None:
    client = make_client(lambda request: httpx.Response(200, text="<html>proxy page</html>"))

    with pytest.raises(ApiError, match="unexpected response body for GET /items/1"):
        client.get(1)
