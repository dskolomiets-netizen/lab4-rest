# Командная строка клиента: python -m client [--url URL] <команда> ...
# Любая ошибка (сеть, 404, 422, битый файл) печатается одной ASCII-строкой в stderr
# с кодом выхода 1 - без трейсбека.

import argparse
import io
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.schemas import ItemCreate, ItemPage, ItemRead
from client.api import DEFAULT_TIMEOUT, DEFAULT_URL, ApiError, ConflictError, LibraryClient
from client.text import to_ascii


def _force_ascii(stream: object) -> None:
    # Любой не-ASCII символ, который всё же дошёл до печати, выводится как \uXXXX
    if isinstance(stream, io.TextIOWrapper):
        stream.reconfigure(encoding="ascii", errors="backslashreplace")


def _duration(text: str) -> int:
    # "3:07" или "187" -> секунды
    minutes, _, seconds = text.rpartition(":")
    try:
        return int(minutes or 0) * 60 + int(seconds)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected M:SS or seconds, got {ascii(text)}") from None


def _clock(seconds: int) -> str:
    minutes, rest = divmod(seconds, 60)
    return f"{minutes}:{rest:02d}"


def _line(item: ItemRead) -> str:
    # #3    Linkin Park - Numb [Meteora, 2003] 3:07
    extra = ", ".join(part for part in (item.album, str(item.year or "")) if part)
    text = f"#{item.id:<4} {', '.join(item.artists)} - {item.title}"
    if extra:
        text += f" [{extra}]"
    return to_ascii(f"{text} {_clock(item.duration_sec)}")


def _print_page(page: ItemPage) -> None:
    if not page.items:
        print("no tracks" if page.total == 0 else f"no tracks on this page (total {page.total})")
        return
    first = page.offset + 1
    print(f"tracks {first}-{page.offset + len(page.items)} of {page.total}:")
    for item in page.items:
        print(_line(item))


def _validation_message(exc: ValidationError) -> str:
    parts = [f"{'.'.join(str(p) for p in e['loc']) or 'track'}: {e['msg']}" for e in exc.errors()]
    return to_ascii("invalid input: " + "; ".join(parts))


def _track_from_args(args: argparse.Namespace, current: ItemRead | None = None) -> ItemCreate:
    # add: всё из аргументов; update: текущий трек с сервера + изменённые поля (PUT - полная замена)
    data: dict[str, Any] = current.model_dump(include=set(ItemCreate.model_fields)) if current else {}
    changes = {
        "title": args.title,
        "artists": args.artist,
        "album": args.album,
        "duration_sec": args.duration,
        "year": args.year,
        "spotify_id": args.spotify_id,
    }
    data.update({key: value for key, value in changes.items() if value is not None})
    # Проверка той же моделью, что на сервере: заведомо плохой трек даже не уходит в сеть
    return ItemCreate.model_validate(data)


def _load_lab3_json(path: Path) -> list[ItemCreate]:
    # Формат файла из ЛР3: python -m spotexport <ссылка> -f json -o tracks.json
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path.name}: expected a JSON array of tracks")
    tracks = []
    for index, raw in enumerate(data):
        try:
            tracks.append(
                ItemCreate(
                    title=raw["title"],
                    artists=raw["artists"],
                    album=raw.get("album", ""),
                    # Как в ЛР3 и в приложении Spotify: доли секунды отбрасываются
                    duration_sec=max(1, int(raw["duration_ms"]) // 1000),
                    spotify_id=raw.get("id"),
                )
            )
        except ValidationError as exc:
            raise ValueError(f"{path.name}, track #{index}: {_validation_message(exc)}") from exc
        except (KeyError, TypeError) as exc:
            raise ValueError(f"{path.name}, track #{index}: missing or bad field {exc}") from exc
    return tracks


def _import(client: LibraryClient, path: Path) -> None:
    created = skipped = 0
    for track in _load_lab3_json(path):
        try:
            item = client.create(track)
        except ConflictError:
            # Повторный импорт того же файла не плодит дубли: spotify_id уникален
            skipped += 1
            print(to_ascii(f"skip: {', '.join(track.artists)} - {track.title} (already in library)"))
            continue
        created += 1
        print(f"added {_line(item)}")
    print(f"imported {created} tracks, skipped {skipped}")


def _add_track_options(parser: argparse.ArgumentParser, required: bool) -> None:
    parser.add_argument("--title", required=required)
    parser.add_argument(
        "--artist", action="append", required=required, help="repeat for several artists"
    )
    parser.add_argument("--duration", type=_duration, required=required, help="M:SS or seconds")
    parser.add_argument("--album")
    parser.add_argument("--year", type=int)
    parser.add_argument("--spotify-id")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m client", description="Client for the Music Library REST API."
    )
    parser.add_argument("--url", default=DEFAULT_URL, help=f"service URL, default {DEFAULT_URL}")
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT, help="seconds per request"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (("list", "list tracks page by page"), ("search", "search by title")):
        sub = commands.add_parser(name, help=help_text)
        if name == "search":
            sub.add_argument("query")
        sub.add_argument("--limit", type=int, default=20)
        sub.add_argument("--offset", type=int, default=0)

    commands.add_parser("get", help="show one track").add_argument("id", type=int)
    _add_track_options(commands.add_parser("add", help="add a track"), required=True)
    update = commands.add_parser("update", help="change fields of a track (PUT)")
    update.add_argument("id", type=int)
    _add_track_options(update, required=False)
    commands.add_parser("delete", help="delete a track").add_argument("id", type=int)
    commands.add_parser(
        "import", help="add tracks from a JSON file exported by lab 3 (spotexport -f json)"
    ).add_argument("file", type=Path)
    return parser


def run(client: LibraryClient, args: argparse.Namespace) -> None:
    match args.command:
        case "list":
            _print_page(client.list_items(args.limit, args.offset))
        case "search":
            _print_page(client.search(args.query, args.limit, args.offset))
        case "get":
            print(_line(client.get(args.id)))
        case "add":
            print(f"created {_line(client.create(_track_from_args(args)))}")
        case "update":
            track = _track_from_args(args, current=client.get(args.id))
            print(f"updated {_line(client.replace(args.id, track))}")
        case "delete":
            client.delete(args.id)
            print(f"deleted #{args.id}")
        case "import":
            _import(client, args.file)


def main(argv: Sequence[str] | None = None) -> int:
    _force_ascii(sys.stdout)
    _force_ascii(sys.stderr)
    args = build_parser().parse_args(argv)
    try:
        with LibraryClient(args.url, timeout=args.timeout) as client:
            run(client, args)
    except ApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ValidationError as exc:
        print(f"error: {_validation_message(exc)}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"error: {to_ascii(str(exc))}", file=sys.stderr)
        return 1
    return 0
