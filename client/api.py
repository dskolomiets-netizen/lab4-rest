# Клиент сервиса на httpx: обязательный таймаут, raise_for_status, понятные ASCII-ошибки
# вместо трейсбека и типизированные результаты (те же Pydantic-модели, что в контракте).
# Повтор запроса (ретрай) с растущей паузой - только для идемпотентных GET/PUT/DELETE
# и только при сбое сети или ответе 5xx. POST не повторяется: если первый запрос
# дошёл, а потерялся лишь ответ, повтор создал бы второй такой же трек.

import time
from types import TracebackType
from typing import Any, Self, TypeVar

import httpx
from pydantic import BaseModel

from app.schemas import ItemCreate, ItemPage, ItemRead
from client.text import to_ascii

DEFAULT_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT = 5.0

M = TypeVar("M", bound=BaseModel)


class ApiError(Exception):
    # Базовая ошибка клиента. Текст всегда ASCII - его можно сразу печатать пользователю
    pass


class NotFoundError(ApiError):  # 404
    pass


class InvalidInputError(ApiError):  # 422
    pass


class ConflictError(ApiError):  # 409: spotify_id уже занят
    pass


class ServerError(ApiError):  # 5xx, остался и после всех повторов
    pass


class ServiceUnavailableError(ApiError):  # сервер не ответил: нет соединения, таймаут
    pass


def _error_detail(response: httpx.Response) -> str:
    # FastAPI кладёт причину в "detail": строку (404, 409) или список ошибок полей (422)
    try:
        detail = response.json()["detail"]
    except (ValueError, KeyError, TypeError):
        return to_ascii(response.text[:200]) or response.reason_phrase
    if isinstance(detail, list):
        parts = []
        for error in detail:
            # loc = ["body", "title"] или ["query", "limit"]: первый элемент - где искать поле
            field = ".".join(str(part) for part in error.get("loc", [])[1:]) or "request"
            parts.append(f"{field}: {error.get('msg', 'invalid')}")
        return to_ascii("; ".join(parts))
    return to_ascii(str(detail))


def _status_error(exc: httpx.HTTPStatusError) -> ApiError:
    response = exc.response
    code = response.status_code
    detail = _error_detail(response)
    if code == 404:
        return NotFoundError(f"not found (404): {detail}")
    if code == 409:
        return ConflictError(f"conflict (409): {detail}")
    if code == 422:
        return InvalidInputError(f"invalid input (422): {detail}")
    if code >= 500:
        return ServerError(f"server error ({code}): {detail}")
    return ApiError(f"unexpected answer {code} {response.reason_phrase}: {detail}")


class LibraryClient:
    def __init__(
        self,
        base_url: str = DEFAULT_URL,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = 2,
        backoff: float = 0.5,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        # transport подменяют в тестах (httpx.MockTransport), в работе он не нужен
        self._http = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)
        self._timeout = timeout
        self._retries = retries
        self._backoff = backoff

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # --- операции сервиса -------------------------------------------------------

    def create(self, data: ItemCreate) -> ItemRead:
        response = self._request("POST", "/items", json=data.model_dump(mode="json"), retry=False)
        return self._parse(ItemRead, response)

    def get(self, item_id: int) -> ItemRead:
        return self._parse(ItemRead, self._request("GET", f"/items/{item_id}"))

    def list_items(self, limit: int = 20, offset: int = 0) -> ItemPage:
        response = self._request("GET", "/items", params={"limit": limit, "offset": offset})
        return self._parse(ItemPage, response)

    def search(self, query: str, limit: int = 20, offset: int = 0) -> ItemPage:
        params: dict[str, str | int] = {"q": query, "limit": limit, "offset": offset}
        return self._parse(ItemPage, self._request("GET", "/items/search", params=params))

    def replace(self, item_id: int, data: ItemCreate) -> ItemRead:
        response = self._request("PUT", f"/items/{item_id}", json=data.model_dump(mode="json"))
        return self._parse(ItemRead, response)

    def delete(self, item_id: int) -> None:
        self._request("DELETE", f"/items/{item_id}")

    # --- транспорт ---------------------------------------------------------------

    def _request(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        params: dict[str, str | int] | None = None,
        retry: bool = True,
    ) -> httpx.Response:
        attempts = 1 + (self._retries if retry else 0)
        for attempt in range(1, attempts + 1):
            last_try = attempt == attempts
            try:
                response = self._http.request(method, url, json=json, params=params)
                # Без этой строки 404 и 500 считались бы обычным ответом
                response.raise_for_status()
                return response
            except httpx.HTTPStatusError as exc:
                # 4xx повторять бессмысленно: виноват запрос, а не сервер
                if exc.response.status_code < 500 or last_try:
                    raise _status_error(exc) from exc
            except httpx.RequestError as exc:
                # Сюда попадают таймауты (TimeoutException) и обрывы соединения
                if last_try:
                    raise ServiceUnavailableError(self._network_message(exc)) from exc
            time.sleep(self._backoff * 2 ** (attempt - 1))
        raise AssertionError("unreachable: the last attempt always returns or raises")

    def _network_message(self, exc: httpx.RequestError) -> str:
        where = str(self._http.base_url).rstrip("/")
        kind = type(exc).__name__
        if isinstance(exc, httpx.TimeoutException):
            return f"{where} did not answer within {self._timeout:g} s ({kind})"
        if isinstance(exc, httpx.ConnectError):
            return f"cannot connect to {where} ({kind}); is the server running?"
        return f"network error while talking to {where} ({kind})"

    @staticmethod
    def _parse(model: type[M], response: httpx.Response) -> M:
        # Ответ проверяется той же моделью, что описывает контракт: не тот JSON - понятная ошибка
        try:
            return model.model_validate(response.json())
        except ValueError as exc:  # JSONDecodeError и pydantic.ValidationError - оба ValueError
            raise ApiError(
                f"unexpected response body for {response.request.method} {response.url.path}"
            ) from exc
