"""Запуск: приложение не должно открываться в устаревшем движке."""

from __future__ import annotations

import sys

import pytest

from knigovert.__main__ import run_window
from knigovert.server import AppState


@pytest.mark.skipif(sys.platform != "win32", reason="выбор движка есть только в Windows")
def test_no_window_without_webview2(tmp_path, monkeypatch):
    from webview.platforms import winforms

    import webview

    def fail(*args, **kwargs):
        raise AssertionError("окно не должно создаваться без WebView2")

    monkeypatch.setattr(winforms, "is_chromium", False)
    monkeypatch.setattr(webview, "create_window", fail)
    state = AppState(settings_file=tmp_path / "settings.json")
    try:
        assert run_window(state, "http://127.0.0.1:1/") is False
    finally:
        state.cleanup()


def test_activity_is_tracked_for_browser_mode(tmp_path):
    from test_server import request

    from knigovert.server import start_server

    state = AppState(settings_file=tmp_path / "settings.json")
    server, port = start_server(state)
    try:
        assert state.last_seen == 0
        assert request(port, "GET", "/api/ping")[0] == 403
        assert state.last_seen == 0, "запрос без доступа не считается активностью"
        assert request(port, "GET", "/api/ping", headers={"X-Token": state.token})[0] == 200
        assert state.last_seen > 0
    finally:
        server.shutdown()
        state.cleanup()
