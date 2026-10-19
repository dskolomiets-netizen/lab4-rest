# Эндпоинты через TestClient: на каждый - позитивный сценарий и негативный (422 / 404).

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import get_repository


def _create(api: TestClient, payload: dict[str, Any]) -> dict[str, Any]:
    response = api.post("/items", json=payload)
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def _error_fields(response: Any) -> list[list[Any]]:
    # loc ошибки без последнего индекса списка: ["body", "artists", 0] -> ["body", "artists"]
    return [error["loc"][:2] for error in response.json()["detail"]]


# --- POST /items -----------------------------------------------------------------


def test_create_returns_201_with_new_id_and_location(api: TestClient, numb: dict[str, Any]) -> None:
    response = api.post("/items", json=numb)

    assert response.status_code == 201
    body = response.json()
    assert body["id"] == 1
    assert {key: body[key] for key in numb} == numb
    assert body["created_at"] == body["updated_at"]
    assert response.headers["location"] == "/items/1"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", ""),
        ("title", "   "),  # только пробелы: обрезаются до пустой строки
        ("title", "x" * 201),
        ("artists", []),
        ("artists", [""]),
        ("duration_sec", 0),
        ("duration_sec", "long"),
        ("year", 1800),
        ("spotify_id", "not-a-spotify-id"),
    ],
)
def test_create_with_invalid_field_gives_422(
    api: TestClient, numb: dict[str, Any], field: str, value: Any
) -> None:
    response = api.post("/items", json={**numb, field: value})

    assert response.status_code == 422
    assert ["body", field] in _error_fields(response)
    assert api.get("/items").json()["total"] == 0  # ничего не сохранилось


def test_create_without_required_field_or_with_id_gives_422(
    api: TestClient, numb: dict[str, Any]
) -> None:
    without_title = {key: value for key, value in numb.items() if key != "title"}
    missing = api.post("/items", json=without_title)
    with_id = api.post("/items", json={**numb, "id": 7})  # id назначает сервер, не клиент

    assert missing.status_code == 422
    assert missing.json()["detail"][0]["type"] == "missing"
    assert with_id.status_code == 422
    assert with_id.json()["detail"][0]["type"] == "extra_forbidden"


def test_create_with_taken_spotify_id_gives_409(api: TestClient, numb: dict[str, Any]) -> None:
    _create(api, numb)

    response = api.post("/items", json={**numb, "title": "Numb (live)"})

    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]


# --- GET /items/{item_id} ----------------------------------------------------------


def test_get_returns_stored_track(api: TestClient, numb: dict[str, Any]) -> None:
    created = _create(api, numb)

    response = api.get(f"/items/{created['id']}")

    assert response.status_code == 200
    assert response.json() == created


def test_get_unknown_id_gives_404_not_500(api: TestClient) -> None:
    response = api.get("/items/999")

    assert response.status_code == 404
    assert response.json() == {"detail": "track 999 not found"}


def test_get_non_integer_id_gives_422(api: TestClient) -> None:
    response = api.get("/items/abc")

    assert response.status_code == 422
    assert _error_fields(response) == [["path", "item_id"]]


# --- GET /items --------------------------------------------------------------------


def test_list_paginates_with_limit_and_offset(
    api: TestClient, numb: dict[str, Any], kino: dict[str, Any]
) -> None:
    ids = [_create(api, track)["id"] for track in (numb, kino, {**kino, "title": "Kukushka"})]

    first = api.get("/items", params={"limit": 2})
    second = api.get("/items", params={"limit": 2, "offset": 2})
    beyond = api.get("/items", params={"offset": 10})

    assert first.status_code == 200
    assert [item["id"] for item in first.json()["items"]] == ids[:2]
    assert first.json()["total"] == 3
    assert [item["id"] for item in second.json()["items"]] == ids[2:]
    assert beyond.json() == {"items": [], "total": 3, "limit": 20, "offset": 10}


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"limit": "ten"}])
def test_list_with_bad_paging_gives_422(api: TestClient, params: dict[str, Any]) -> None:
    response = api.get("/items", params=params)

    assert response.status_code == 422
    assert _error_fields(response)[0] == ["query", next(iter(params))]


# --- GET /items/search -------------------------------------------------------------


def test_search_finds_substring_ignoring_case_cyrillic_too(
    api: TestClient, numb: dict[str, Any], kino: dict[str, Any]
) -> None:
    numb_id = _create(api, numb)["id"]
    kino_id = _create(api, kino)["id"]

    latin = api.get("/items/search", params={"q": "nUm"})
    # "ГРУППА" заглавными, в базе "Группа крови"
    cyrillic = api.get("/items/search", params={"q": "ГРУППА"})

    assert latin.status_code == 200
    assert [item["id"] for item in latin.json()["items"]] == [numb_id]
    assert [item["id"] for item in cyrillic.json()["items"]] == [kino_id]


def test_search_without_matches_is_empty_200(api: TestClient, numb: dict[str, Any]) -> None:
    _create(api, numb)

    response = api.get("/items/search", params={"q": "zzz"})

    # Пустой результат поиска - не ошибка: 200 и пустой список, а не 404
    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.json()["total"] == 0


@pytest.mark.parametrize("params", [{}, {"q": ""}, {"q": "x" * 101}])
def test_search_without_valid_query_gives_422(api: TestClient, params: dict[str, Any]) -> None:
    response = api.get("/items/search", params=params)

    assert response.status_code == 422
    assert _error_fields(response) == [["query", "q"]]


# --- PUT /items/{item_id} ----------------------------------------------------------


def test_replace_changes_fields_and_keeps_created_at(api: TestClient, numb: dict[str, Any]) -> None:
    created = _create(api, numb)

    response = api.put(f"/items/{created['id']}", json={**numb, "title": "Numb (remastered)"})

    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Numb (remastered)"
    assert body["created_at"] == created["created_at"]
    assert body["updated_at"] >= created["updated_at"]
    assert api.get(f"/items/{created['id']}").json() == body


def test_replace_unknown_id_gives_404(api: TestClient, numb: dict[str, Any]) -> None:
    response = api.put("/items/999", json=numb)

    assert response.status_code == 404


def test_replace_with_invalid_body_gives_422(api: TestClient, numb: dict[str, Any]) -> None:
    created = _create(api, numb)

    response = api.put(f"/items/{created['id']}", json={**numb, "duration_sec": -1})

    assert response.status_code == 422
    assert api.get(f"/items/{created['id']}").json() == created  # трек не изменился


# --- DELETE /items/{item_id} -------------------------------------------------------


def test_delete_gives_204_and_track_disappears(api: TestClient, numb: dict[str, Any]) -> None:
    created = _create(api, numb)

    response = api.delete(f"/items/{created['id']}")

    assert response.status_code == 204
    assert response.content == b""
    assert api.get(f"/items/{created['id']}").status_code == 404


def test_delete_unknown_id_gives_404(api: TestClient) -> None:
    response = api.delete("/items/999")

    assert response.status_code == 404


# --- ASCII, документация, изоляция -------------------------------------------------


def test_responses_are_ascii_and_keep_cyrillic(api: TestClient, kino: dict[str, Any]) -> None:
    response = api.post("/items", json=kino)
    invalid = api.post("/items", json={**kino, "year": "давно"})

    # В теле ответа \uXXXX, после разбора JSON - исходная кириллица
    assert response.content.isascii()
    assert response.json()["title"] == kino["title"]
    assert invalid.status_code == 422
    assert invalid.content.isascii()


def test_openapi_schema_and_swagger_ui_are_served(api: TestClient) -> None:
    schema = api.get("/openapi.json")
    docs = api.get("/docs")

    assert schema.status_code == 200
    paths = schema.json()["paths"]
    assert set(paths) == {"/items", "/items/search", "/items/{item_id}"}
    assert set(paths["/items/{item_id}"]) == {"get", "put", "delete"}
    assert {"ItemCreate", "ItemRead", "ItemPage"} <= set(schema.json()["components"]["schemas"])
    assert docs.status_code == 200
    assert "swagger-ui" in docs.text


def test_working_database_is_never_opened(api: TestClient, numb: dict[str, Any]) -> None:
    _create(api, numb)

    # Настоящая get_repository ни разу не вызывалась: тесты работают только с tmp_path
    assert get_repository.cache_info().currsize == 0
