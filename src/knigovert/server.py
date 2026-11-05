"""Локальный сервер интерфейса. Слушает только 127.0.0.1 и ничего не отправляет в сеть."""

from __future__ import annotations

import hmac
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional

from . import APP_NAME, __version__
from .convert import OUTPUTS, detect_format, read_book, safe_filename, stem_of, unique_path, write_book
from .formats.markup import blocks_to_html
from .formats.pdf_out import merge_pages
from .jobs import Job, JobQueue
from .library import Library
from .model import Book, ConversionError, Meta, make_image, prepare_for_write
from .paths import WEB_DIR

CHUNK = 1024 * 1024
CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; font-src 'self'; "
    "script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
)


def default_output_dir() -> Path:
    """Папка «Документы» (учитывает перенос в OneDrive) и подпапка приложения."""
    documents = Path.home() / "Documents"
    if sys.platform == "win32":
        import ctypes

        buffer = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buffer) == 0 and buffer.value:
            documents = Path(buffer.value)
    return documents / APP_NAME


def settings_path() -> Path:
    base = os.environ.get("APPDATA")
    folder = Path(base) / "Knigovert" if base else Path.home() / ".knigovert"
    return folder / "settings.json"


class AppState:
    def __init__(self, settings_file: Optional[Path] = None):
        self.token = secrets.token_urlsafe(32)
        self.temp_dir = Path(tempfile.mkdtemp(prefix="knigovert-"))
        self.files: dict[str, dict] = {}
        self.outputs: set[str] = set()
        self.library = Library()
        self.jobs = JobQueue()
        self.settings_file = settings_file or settings_path()
        self.settings = self._load_settings()
        self.dirty = False
        self.last_seen = 0.0
        self.pick_folder: Optional[Callable[[str], Optional[str]]] = None

    def _load_settings(self) -> dict:
        try:
            data = json.loads(self.settings_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_settings(self) -> None:
        try:
            self.settings_file.parent.mkdir(parents=True, exist_ok=True)
            self.settings_file.write_text(json.dumps(self.settings, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass  # Настройки не важнее работы: без них приложение тоже запустится.

    @property
    def output_dir(self) -> Path:
        value = self.settings.get("output_dir")
        return Path(value) if value else default_output_dir()

    def cleanup(self) -> None:
        self.jobs.shutdown()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def store_upload(self, name: str, reader, length: int) -> dict:
        file_id = secrets.token_hex(8)
        path = self.temp_dir / file_id
        remaining = length
        with path.open("wb") as handle:
            while remaining > 0:
                chunk = reader.read(min(CHUNK, remaining))
                if not chunk:
                    break
                handle.write(chunk)
                remaining -= len(chunk)
        info = {"id": file_id, "name": Path(name.replace("\\", "/")).name or "файл", "size": length, "path": str(path)}
        try:
            info["format"] = detect_format(path, info["name"])
            info["error"] = ""
        except ConversionError as error:
            info["format"] = ""
            info["error"] = str(error)
        self.files[file_id] = info
        return {key: info[key] for key in ("id", "name", "size", "format", "error")}

    def file_info(self, file_id: str) -> dict:
        info = self.files.get(file_id)
        if info is None:
            raise ConversionError("Файл не найден. Добавьте его ещё раз.")
        return info

    # --- Работа ---------------------------------------------------------

    def _output_path(self, name: str, target: str) -> Path:
        folder = self.output_dir
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise ConversionError(f"Не удалось создать папку {folder}: {error}") from error
        return unique_path(folder, safe_filename(name), OUTPUTS[target].extension)

    def _finish(self, job: Job, path: Path, warnings: list[str]) -> dict:
        self.outputs.add(str(path))
        job.warnings = list(warnings)
        return {"path": str(path), "name": path.name, "size": path.stat().st_size}

    @staticmethod
    def _reporter(job: Job):
        def report(stage: str, progress: Optional[float] = None) -> None:
            job.stage = stage
            job.progress = progress

        return report

    def convert(self, file_id: str, target: str, options: dict) -> Job:
        info = self.file_info(file_id)
        if info["error"]:
            raise ConversionError(info["error"])
        if target not in OUTPUTS:
            raise ConversionError("Неизвестный формат.")

        def work(job: Job) -> dict:
            report = self._reporter(job)
            report("Читаю файл", None)
            book = read_book(Path(info["path"]), info["name"], report)
            report("Создаю файл", None)
            path = self._output_path(stem_of(info["name"]), target)
            try:
                write_book(book, path, target, options, report)
            except Exception:
                path.unlink(missing_ok=True)
                raise
            return self._finish(job, path, book.warnings)

        return self.jobs.submit(work)

    def builder_add(self, file_id: str, split: bool) -> Job:
        info = self.file_info(file_id)
        if info["error"]:
            raise ConversionError(info["error"])

        def work(job: Job) -> dict:
            report = self._reporter(job)
            report("Читаю файл", None)
            book = read_book(Path(info["path"]), info["name"], report)
            chapters = self.library.add_book(book, split=split, source_name=stem_of(info["name"]))
            job.warnings = list(book.warnings)
            self.dirty = True
            meta = book.meta
            return {
                "chapters": [self.library.describe(chapter) for chapter in chapters],
                "meta": {
                    "title": meta.title,
                    "authors": ", ".join(meta.authors),
                    "annotation": meta.annotation,
                    "lang": meta.lang,
                    "cover_id": meta.cover_id,
                },
            }

        return self.jobs.submit(work)

    def builder_export(self, payload: dict) -> Job:
        target = payload.get("format", "pdf")
        if target not in OUTPUTS:
            raise ConversionError("Неизвестный формат.")
        raw_meta = payload.get("meta") or {}
        authors = [item.strip() for item in re.split(r"[;,]", str(raw_meta.get("authors", ""))) if item.strip()]
        meta = Meta(
            title=str(raw_meta.get("title", "")).strip() or "Без названия",
            authors=authors,
            annotation=str(raw_meta.get("annotation", "")).strip(),
            lang=str(raw_meta.get("lang", "")).strip()[:2],
            cover_id=raw_meta.get("cover_id") or None,
        )
        order = payload.get("chapters") or []
        options = payload.get("options") or {}
        # Ошибки в составе книги видны сразу, а не после ожидания.
        book = self.library.compose(meta, order)

        def work(job: Job) -> dict:
            report = self._reporter(job)
            report("Собираю книгу", None)
            path = self._output_path(meta.title, target)
            keep_pages = (
                target == "pdf"
                and options.get("keep_pdf_pages", True)
                and all(chapter.pdf_pages for chapter in book.chapters)
            )
            try:
                if keep_pages:
                    merge_pages(book, path, report)
                else:
                    write_book(book, path, target, options, report)
            except Exception:
                path.unlink(missing_ok=True)
                raise
            self.dirty = False
            return self._finish(job, path, book.warnings)

        return self.jobs.submit(work)

    def chapter_preview(self, chapter_id: str) -> dict:
        chapter = self.library.get(chapter_id)
        book = Book(images=self.library.images, notes=self.library.notes, chapters=[chapter])
        prepared = prepare_for_write(book)
        token = urllib.parse.quote(self.token)
        # Весь текст экранируется в blocks_to_html; ссылки допускаются только http(s) и mailto.
        lines = blocks_to_html(
            chapter.blocks,
            prepared,
            lambda image_id: f"/api/image/{image_id}?t={token}",
            lambda label: f"#preview-note-{label}",
            numbered_from=0,
        )
        notes = []
        for note in prepared.notes.values():
            inner = blocks_to_html(
                note.blocks, prepared, lambda image_id: f"/api/image/{image_id}?t={token}", lambda label: f"#preview-note-{label}"
            )
            notes.append({"label": note.label, "html": "".join(inner)})
        return {"html": lines, "notes": notes, "title": chapter.title}

    def open_path(self, path: str, reveal: bool) -> None:
        # Открываем только то, что создало само приложение, и папку сохранения.
        if path not in self.outputs and path != str(self.output_dir):
            raise ConversionError("Этот файл создан не приложением.")
        target = Path(path)
        if not target.exists():
            if path == str(self.output_dir):
                target.mkdir(parents=True, exist_ok=True)
            else:
                raise ConversionError("Файл уже удалён или перемещён.")
        if sys.platform == "win32":
            if reveal and target.is_file():
                subprocess.Popen(["explorer", f"/select,{target}"])
            else:
                os.startfile(str(target))  # noqa: S606 - путь проверен выше
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(target)] if reveal else ["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target.parent if reveal else target)])


class _Handler(BaseHTTPRequestHandler):
    server_version = "Knigovert"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    # Заголовок и тело уходят отдельно: без этого Windows придерживает ответ до 200 мс.
    disable_nagle_algorithm = True
    state: AppState
    port: int

    def log_message(self, format, *args):  # noqa: A002
        pass

    def _json(self, data, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, message: str, status: int = 400) -> None:
        self._json({"error": message}, status)

    def _host_allowed(self) -> bool:
        # Защита от DNS rebinding: чужой сайт не сможет притвориться локальным.
        return self.headers.get("Host", "") in (f"127.0.0.1:{self.port}", f"localhost:{self.port}")

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        return origin is None or origin in (f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}")

    def _authorized(self, query: dict) -> bool:
        token = self.headers.get("X-Token") or query.get("t", [""])[0]
        return hmac.compare_digest(token.encode("utf-8"), self.state.token.encode("utf-8"))

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError as error:
            raise ConversionError("Некорректный запрос.") from error
        if not isinstance(data, dict):
            raise ConversionError("Некорректный запрос.")
        return data

    def _serve_static(self, relative: str) -> None:
        relative = relative.lstrip("/") or "index.html"
        root = WEB_DIR.resolve()
        target = (root / relative).resolve()
        if root not in target.parents or not target.is_file():
            self._error("Не найдено", 404)
            return
        data = target.read_bytes()
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix == ".js":
            content_type = "text/javascript"
        if target.suffix == ".ttf":
            content_type = "font/ttf"
        if target.name == "index.html":
            data = data.replace(b"{{TOKEN}}", self.state.token.encode("ascii"))
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8" if content_type.startswith("text") else content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if not self._host_allowed():
            self._error("Запрещено", 403)
            return
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if not path.startswith("/api/"):
            if path.startswith("/fonts/"):
                # Шрифты интерфейса лежат вместе со шрифтами для PDF.
                self._serve_font(path.rsplit("/", 1)[-1])
            else:
                self._serve_static(path)
            return
        if not self._authorized(query):
            self._error("Нет доступа", 403)
            return
        self.state.last_seen = time.monotonic()
        try:
            self._api_get(path)
        except ConversionError as error:
            self._error(str(error))

    def _serve_font(self, name: str) -> None:
        from .paths import FONTS_DIR

        if not re.fullmatch(r"[A-Za-z0-9_-]+\.ttf", name) or not (FONTS_DIR / name).is_file():
            self._error("Не найдено", 404)
            return
        data = (FONTS_DIR / name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "font/ttf")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def _api_get(self, path: str) -> None:
        state = self.state
        if path == "/api/ping":
            self._json({"ok": True})
            return
        if path == "/api/info":
            self._json(
                {
                    "name": APP_NAME,
                    "version": __version__,
                    "output_dir": str(state.output_dir),
                    "native_dialogs": state.pick_folder is not None,
                    "formats": [{"key": item.key, "label": item.label, "ext": item.extension} for item in OUTPUTS.values()],
                    "options": state.settings.get("options", {}),
                }
            )
            return
        match = re.fullmatch(r"/api/jobs/([0-9a-f]+)", path)
        if match:
            job = state.jobs.get(match.group(1))
            if job is None:
                self._error("Задача не найдена", 404)
            else:
                self._json(job.to_dict())
            return
        match = re.fullmatch(r"/api/builder/chapter/([0-9a-f]+)", path)
        if match:
            self._json(state.chapter_preview(match.group(1)))
            return
        match = re.fullmatch(r"/api/image/([0-9a-f]+)", path)
        if match:
            image = state.library.images.get(match.group(1))
            if image is None:
                self._error("Не найдено", 404)
                return
            self.send_response(200)
            self.send_header("Content-Type", image.mime)
            self.send_header("Content-Length", str(len(image.data)))
            self.send_header("Cache-Control", "max-age=3600")
            self.end_headers()
            self.wfile.write(image.data)
            return
        self._error("Не найдено", 404)

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if not self._host_allowed() or not self._origin_allowed() or not self.headers.get("X-Token") or not self._authorized(query):
            # Тело не читаем, поэтому соединение дальше использовать нельзя.
            self.close_connection = True
            self._error("Нет доступа", 403)
            return
        try:
            self._api_post(parsed.path)
        except ConversionError as error:
            self._error(str(error))

    def _api_post(self, path: str) -> None:
        state = self.state
        if path == "/api/files":
            name = urllib.parse.unquote(self.headers.get("X-Filename", "файл"))
            length = int(self.headers.get("Content-Length") or 0)
            self._json(state.store_upload(name, self.rfile, length))
            return
        data = self._read_json()
        if path == "/api/convert":
            job = state.convert(str(data.get("file_id")), str(data.get("target")), data.get("options") or {})
            self._json({"job_id": job.id})
        elif path == "/api/builder/add":
            job = state.builder_add(str(data.get("file_id")), bool(data.get("split", True)))
            self._json({"job_id": job.id})
        elif path == "/api/builder/merge":
            merged = state.library.merge(
                [str(item) for item in data.get("ids", [])],
                title=data.get("title"),
                keep_titles=bool(data.get("keep_titles", True)),
                titles=data.get("titles"),
            )
            state.dirty = True
            self._json(state.library.describe(merged))
        elif path == "/api/builder/split":
            first, second = state.library.split(str(data.get("id")), int(data.get("at", 0)), str(data.get("title", "")))
            state.dirty = True
            self._json({"first": state.library.describe(first), "second": state.library.describe(second)})
        elif path == "/api/builder/export":
            job = state.builder_export(data)
            self._json({"job_id": job.id})
        elif path == "/api/builder/cover":
            info = state.file_info(str(data.get("file_id")))
            image = make_image(Path(info["path"]).read_bytes())
            if image is None:
                raise ConversionError("Обложкой может быть только картинка: JPG, PNG, WEBP.")
            self._json({"cover_id": state.library.add_cover(image)})
        elif path == "/api/dirty":
            state.dirty = bool(data.get("dirty"))
            self._json({"ok": True})
        elif path == "/api/open":
            state.open_path(str(data.get("path")), bool(data.get("reveal")))
            self._json({"ok": True})
        elif path == "/api/output-dir":
            self._set_output_dir(data)
        elif path == "/api/options":
            options = data.get("options")
            if isinstance(options, dict):
                state.settings["options"] = options
                state.save_settings()
            self._json({"ok": True})
        else:
            self._error("Не найдено", 404)

    def _set_output_dir(self, data: dict) -> None:
        state = self.state
        chosen = data.get("path")
        if not chosen:
            if state.pick_folder is None:
                self._json({"unsupported": True, "output_dir": str(state.output_dir)})
                return
            chosen = state.pick_folder(str(state.output_dir))
            if not chosen:
                self._json({"output_dir": str(state.output_dir), "cancelled": True})
                return
        path = Path(str(chosen)).expanduser()
        if not path.is_absolute():
            raise ConversionError("Укажите полный путь к папке.")
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise ConversionError(f"Не удалось открыть папку: {error}") from error
        state.settings["output_dir"] = str(path)
        state.save_settings()
        self._json({"output_dir": str(path)})


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Обрыв соединения при закрытии окна — обычное дело, не ошибка.
        pass


def start_server(state: AppState, port: int = 0) -> tuple[_Server, int]:
    handler = type("Handler", (_Handler,), {"state": state})
    server = _Server(("127.0.0.1", port), handler)
    actual_port = server.server_address[1]
    handler.port = actual_port
    threading.Thread(target=server.serve_forever, name="knigovert-server", daemon=True).start()
    return server, actual_port
