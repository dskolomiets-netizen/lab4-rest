# Music Library API

REST-сервис музыкальной библиотеки на FastAPI и клиент к нему на httpx.
Ресурс - трек (та же сущность, что `Track` в ЛР3): CRUD, список с пагинацией
`limit`/`offset`, поиск по подстроке названия. Вход и выход описаны
отдельными Pydantic-моделями, данные лежат в SQLite и переживают перезапуск,
документация генерируется автоматически на `/docs`. Лабораторная работа №4.

Клиент умеет импортировать JSON, который выгружает ЛР3
(`python -m spotexport <ссылка> -f json`), так что две лабы работают в паре:
ЛР3 достаёт треки из Spotify, ЛР4 хранит их и отдаёт по REST.

## Структура

```
app/
  schemas.py      контракт: ItemCreate (вход, без id), ItemRead (выход: + id, created_at,
                  updated_at), ItemPage (страница + total), ErrorMessage
  repository.py   слой хранения: протокол TrackRepository + SqliteTrackRepository (sqlite3)
  main.py         FastAPI-приложение: эндпоинты, коды ответов, ASCII-JSON
client/
  api.py          LibraryClient на httpx: timeout, raise_for_status, ретраи, ошибки
  cli.py          командная строка: python -m client ...
  text.py         to_ascii() из ЛР3: весь вывод клиента - ASCII
examples/
  tracks.json     4 трека в формате экспорта ЛР3
tests/            pytest, 47 тестов; у каждого теста своя временная БД
pyproject.toml    mypy (strict) и pytest
requirements.txt  точные версии зависимостей
```

## Установка

Нужен Python 3.11+.

```bash
python3 -m venv .venv
source .venv/bin/activate        # Linux / macOS
# .venv\Scripts\activate         # Windows
pip install -r requirements.txt
```

## Запуск сервиса

```bash
uvicorn app.main:app --reload
```

- Swagger UI: <http://127.0.0.1:8000/docs> (корень `/` перенаправляет туда же)
- OpenAPI-схема: <http://127.0.0.1:8000/openapi.json>

База создаётся при первом запросе в `data/tracks.db` (папка в `.gitignore`).
Другой файл можно задать переменной окружения `TRACKS_DB`.

## Эндпоинты

| Метод и путь | Что делает | Успех | Ошибки |
|---|---|---|---|
| `POST /items` | добавить трек | 201 + тело + `Location` | 422, 409 (`spotify_id` занят) |
| `GET /items?limit=&offset=` | список по страницам | 200 | 422 (`limit` вне 1..100, `offset` < 0) |
| `GET /items/search?q=` | поиск по подстроке названия | 200 (пустой список - тоже 200) | 422 (нет `q`) |
| `GET /items/{item_id}` | один трек | 200 | 404, 422 (id не число) |
| `PUT /items/{item_id}` | заменить все поля | 200 | 404, 422, 409 |
| `DELETE /items/{item_id}` | удалить | 204, пустое тело | 404 |

Поля трека (`ItemCreate`):

| Поле | Тип | Ограничения |
|---|---|---|
| `title` | строка | обязательно, 1..200 символов, пробелы по краям обрезаются |
| `artists` | список строк | обязательно, 1..10 имён, каждое 1..100 символов |
| `album` | строка | до 200 символов, по умолчанию `""` |
| `duration_sec` | целое | обязательно, 1..86400 |
| `year` | целое или `null` | 1900..2100 |
| `spotify_id` | строка или `null` | ровно 22 символа base62, уникален в библиотеке |

Любое другое поле (в том числе `id`) - 422: id назначает сервер.

## Примеры запросов

curl (в Windows PowerShell пишите `curl.exe`: просто `curl` там - другая команда):

```bash
curl -i -X POST http://127.0.0.1:8000/items -H "Content-Type: application/json" \
     -d '{"title": "Numb", "artists": ["Linkin Park"], "album": "Meteora", "duration_sec": 187}'
curl "http://127.0.0.1:8000/items?limit=2&offset=0"
curl "http://127.0.0.1:8000/items/search?q=numb"
curl -i http://127.0.0.1:8000/items/999                # 404 {"detail": "track 999 not found"}
curl -i -X POST http://127.0.0.1:8000/items -H "Content-Type: application/json" \
     -d '{"title": "", "artists": [], "duration_sec": -1}'   # 422 с причиной по каждому полю
```

Клиент (сервис должен быть запущен):

```bash
python -m client import examples/tracks.json
python -m client list --limit 3
python -m client search "группа"
python -m client add --title "Kukushka" --artist "Kino" --duration 6:39 --year 1990
python -m client update 5 --year 1991
python -m client get 5
python -m client delete 5
python -m client --url http://127.0.0.1:8000 --timeout 3 list
```

```
added #1    Kino - Gruppa krovi [Gruppa krovi] 4:44
added #2    Kino - V nashikh glazakh [Gruppa krovi] 3:34
added #3    Linkin Park - Numb [Meteora] 3:07
added #4    JAY-Z, Linkin Park - Numb / Encore [Numb / Encore: MTV Ultimate Mash-Ups Presents Collision Course] 3:25
imported 4 tracks, skipped 0
```

Ошибки клиент печатает одной строкой в stderr и выходит с кодом 1, без трейсбека:

```
error: not found (404): track 5 not found
error: invalid input (422): limit: Input should be greater than or equal to 1
error: cannot connect to http://127.0.0.1:8099 (ConnectError); is the server running?
error: invalid input: duration_sec: Input should be greater than or equal to 1
```

Последняя ошибка найдена ещё до запроса: клиент проверяет ввод той же моделью
`ItemCreate`, что и сервер.

## Как устроено

- **Отдельные модели входа и выхода.** `ItemRead` наследует `ItemCreate` и
  добавляет то, что генерирует сервер: `id`, `created_at`, `updated_at`.
  `response_model=ItemRead` гарантирует, что наружу не уйдёт лишнее.
- **Коды ответов.** 201 + `Location` при создании, 404 через `HTTPException`
  (отсутствующий трек никогда не даёт 500), 422 Pydantic выдаёт сам, 204 при
  удалении, 409 при повторном `spotify_id`.
- **Порядок маршрутов.** `/items/search` объявлен раньше `/items/{item_id}`,
  иначе слово `search` попало бы в `item_id` и получился бы 422.
- **Репозиторий.** Обработчики видят только протокол `TrackRepository`; SQL и
  файл БД спрятаны в `SqliteTrackRepository`. На каждую операцию - своё
  соединение: обычные `def`-обработчики FastAPI вызывает из пула потоков.
- **Поиск.** Встроенные `LIKE`/`lower()` в SQLite понимают регистр только у
  латиницы, поэтому хранится `title_fold = title.casefold()`, а подстрока
  ищется через `instr()`: `%` и `_` в запросе остаются обычными символами.
- **id не переиспользуются** (`AUTOINCREMENT`): после удаления `/items/3`
  навсегда отвечает 404, а не отдаёт чужой трек.
- **ASCII.** JSON-ответы сервиса (и ошибки 422) пишутся с `ensure_ascii`:
  кириллица идёт как `\u0413...` - это валидный JSON, клиент получает исходные
  строки. Клиент печатает транслитерацию (`to_ascii` из ЛР3).
- **Устойчивый клиент.** Таймаут на каждый запрос (по умолчанию 5 с),
  `raise_for_status()` после каждого ответа, ошибки httpx превращаются в
  понятные исключения (`NotFoundError`, `InvalidInputError`, `ConflictError`,
  `ServerError`, `ServiceUnavailableError`). GET/PUT/DELETE повторяются до 3 раз
  с паузой 0.5 с, 1 с - только при сбое сети и 5xx. POST не повторяется:
  если потерялся только ответ, повтор создал бы дубль.

## Тесты и проверка типов

```bash
python -m pytest     # 47 тестов
python -m mypy       # strict: app, client, tests
```

- `tests/test_api.py` - каждый эндпоинт через `fastapi.testclient.TestClient`:
  позитив и негатив (422 на кривой ввод, 404 на чужой id).
- `tests/test_repository.py` - данные переживают перезапуск, id не
  переиспользуются, `%` в поиске не шаблон.
- `tests/test_client.py` - клиент против настоящего uvicorn на свободном порту,
  настоящий таймаут на "молчащем" сокете, ретраи и сетевые ошибки через
  `httpx.MockTransport`, команды CLI.

Изоляция: фикстура `repo` создаёт БД в `tmp_path`, фикстура `api` подменяет
зависимость `get_repository` через `app.dependency_overrides`. Рабочая
`data/tracks.db` в тестах не открывается - это проверяет отдельный тест.

Starlette 1.x советует для `TestClient` пакет `httpx2`; задание требует
`httpx`, и с ним `TestClient` полностью работает, поэтому предупреждение
отключено в `pyproject.toml`.

## Маршрут для защиты

| Критерий | Где смотреть |
|---|---|
| Эндпоинты CRUD и поиска, коды 201/200/404 | `app/main.py` |
| Модели входа/выхода, ограничения полей | `app/schemas.py` - `ItemCreate`, `ItemRead` |
| 422 на некорректный ввод | `tests/test_api.py` - `test_create_with_invalid_field_gives_422` и др. |
| Хранилище за слоем-репозиторием | `app/repository.py`, зависимость `get_repository` в `app/main.py` |
| Данные переживают перезапуск | `tests/test_repository.py` - `test_data_survives_restart` |
| Клиент: timeout, ошибки сети и HTTP, типы | `client/api.py` - `LibraryClient._request` |
| Позитив + негатив на каждый эндпоинт | `tests/test_api.py` |
| Изолированное хранилище в тестах | `tests/conftest.py` - фикстуры `repo`, `api` |
| `/docs` и `/openapi.json` | `tests/test_api.py` - `test_openapi_schema_and_swagger_ui_are_served` |
