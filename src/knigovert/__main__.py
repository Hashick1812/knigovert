"""Запуск приложения: окно на встроенном движке Windows, при его отсутствии — браузер."""

from __future__ import annotations

import argparse
import atexit
import sys
import time
import webbrowser

from . import APP_NAME
from .server import AppState, start_server

# Фоновые вкладки браузер будит редко, поэтому запас большой.
BROWSER_IDLE_LIMIT = 180


def run_window(state: AppState, url: str) -> bool:
    """Открывает окно приложения. Возвращает False, если окно создать не удалось."""
    try:
        import webview

        if sys.platform == "win32":
            from webview.platforms import winforms

            # Без WebView2 библиотека молча берёт устаревший движок Internet Explorer,
            # в котором интерфейс не работает. Браузер в этом случае намного лучше.
            if not winforms.is_chromium:
                return False
    except Exception:
        return False

    window = webview.create_window(APP_NAME, url, width=1180, height=800, min_size=(860, 560), text_select=True)

    def pick_folder(start: str):
        result = window.create_file_dialog(webview.FileDialog.FOLDER, directory=start)
        return result[0] if result else None

    def confirm_close():
        # Несохранённая сборка книги исчезнет вместе с окном: спрашиваем.
        if not state.dirty:
            return True
        return window.create_confirmation_dialog(
            "Закрыть приложение?", "Собранная книга ещё не сохранена и будет потеряна."
        )

    state.pick_folder = pick_folder
    window.events.closing += confirm_close
    try:
        webview.start(private_mode=True)
        return True
    except Exception:
        state.pick_folder = None
        return False


def main() -> int:
    parser = argparse.ArgumentParser(prog="knigovert", description=f"{APP_NAME}: конвертер и сборщик книг без интернета")
    parser.add_argument("--browser", action="store_true", help="открыть в браузере вместо отдельного окна")
    parser.add_argument("--server-only", action="store_true", help="только запустить локальный сервер")
    parser.add_argument("--port", type=int, default=0, help="порт (по умолчанию выбирается свободный)")
    args = parser.parse_args()

    state = AppState()
    atexit.register(state.cleanup)
    server, port = start_server(state, args.port)
    url = f"http://127.0.0.1:{port}/"

    in_browser = False
    if args.server_only:
        print(f"{APP_NAME}: {url}", flush=True)
    elif not args.browser and run_window(state, url):
        server.shutdown()
        return 0
    else:
        in_browser = True
        webbrowser.open(url)
        print(f"{APP_NAME} открыт в браузере: {url}", flush=True)
    try:
        while True:
            time.sleep(1)
            # У собранного приложения нет консоли: без этого процесс остался бы работать
            # после закрытия вкладки. Открытая страница регулярно подаёт сигнал.
            if in_browser and state.last_seen and time.monotonic() - state.last_seen > BROWSER_IDLE_LIMIT:
                break
    except KeyboardInterrupt:
        pass
    server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
