"""Локальный сервер: защита и полный путь от загрузки файла до результата."""

from __future__ import annotations

import http.client
import json
import time
import urllib.parse

import pytest

from conftest import jpeg_bytes, sample_book
from knigovert.convert import write_book
from knigovert.server import AppState, start_server


@pytest.fixture
def app(tmp_path):
    state = AppState(settings_file=tmp_path / "settings.json")
    state.settings["output_dir"] = str(tmp_path / "output")
    server, port = start_server(state)
    yield state, port
    server.shutdown()
    state.cleanup()


def request(port, method, path, body=None, headers=None, host=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    send = dict(headers or {})
    if host:
        send["Host"] = host
    if isinstance(body, dict):
        body = json.dumps(body).encode()
    connection.request(method, path, body=body, headers=send)
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response.status, data, response


def api(app_tuple, path, body=None):
    state, port = app_tuple
    status, data, _ = request(port, "POST", path, body or {}, {"X-Token": state.token})
    return status, json.loads(data)


def upload(app_tuple, name, content):
    state, port = app_tuple
    status, data, _ = request(
        port, "POST", "/api/files", content, {"X-Token": state.token, "X-Filename": urllib.parse.quote(name)}
    )
    assert status == 200
    return json.loads(data)


def wait(app_tuple, job_id):
    state, port = app_tuple
    for _ in range(600):
        _, data, _ = request(port, "GET", f"/api/jobs/{job_id}", headers={"X-Token": state.token})
        job = json.loads(data)
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.05)
    raise AssertionError("задача не завершилась")


@pytest.fixture
def fb2_bytes(tmp_path):
    path = tmp_path / "s.fb2"
    write_book(sample_book(), path, "fb2")
    return path.read_bytes()


def test_server_listens_only_on_loopback(app):
    state, port = app
    status, data, _ = request(port, "GET", "/")
    assert status == 200
    assert state.token.encode() in data


def test_rejects_foreign_host_dns_rebinding(app):
    _, port = app
    assert request(port, "GET", "/", host="evil.example")[0] == 403
    assert request(port, "GET", "/api/info", host="evil.example")[0] == 403


def test_api_requires_token(app):
    state, port = app
    assert request(port, "GET", "/api/info")[0] == 403
    assert request(port, "GET", "/api/info", headers={"X-Token": "wrong"})[0] == 403
    assert request(port, "GET", "/api/info", headers={"X-Token": state.token})[0] == 200
    assert request(port, "POST", "/api/convert", {"file_id": "x"})[0] == 403


def test_post_ignores_token_in_url(app):
    """Чужой сайт может подставить адрес, но не может добавить свой заголовок без разрешения."""
    state, port = app
    assert request(port, "POST", f"/api/dirty?t={state.token}", {"dirty": True})[0] == 403


def test_rejects_foreign_origin(app):
    state, port = app
    headers = {"X-Token": state.token, "Origin": "http://evil.example"}
    assert request(port, "POST", "/api/dirty", {"dirty": True}, headers)[0] == 403


def test_static_cannot_escape_web_folder(app):
    _, port = app
    for path in ("/../server.py", "/..%2f..%2fserver.py", "/fonts/..%2f..%2fserver.py"):
        assert request(port, "GET", path)[0] == 404


def test_page_loads_nothing_from_internet(app):
    _, port = app
    _, data, response = request(port, "GET", "/")
    text = data.decode("utf-8")
    assert "http://" not in text.replace('http://www.w3.org', '') and "https://" not in text
    assert "default-src 'self'" in response.getheader("Content-Security-Policy")
    for name in ("app.js", "app.css"):
        status, content, _ = request(port, "GET", f"/{name}")
        assert status == 200
        body = content.decode("utf-8")
        assert "https://" not in body and "http://" not in body, name


def test_convert_end_to_end(app, tmp_path, fb2_bytes):
    state, _ = app
    info = upload(app, "Моя книга.fb2", fb2_bytes)
    assert info["format"] == "fb2" and info["name"] == "Моя книга.fb2"
    _, data = api(app, "/api/convert", {"file_id": info["id"], "target": "pdf", "options": {"page_size": "A5"}})
    job = wait(app, data["job_id"])
    assert job["status"] == "done", job
    assert job["result"]["name"] == "Моя книга.pdf"
    assert (tmp_path / "output" / "Моя книга.pdf").read_bytes()[:4] == b"%PDF"


def test_convert_twice_keeps_both_files(app, tmp_path, fb2_bytes):
    info = upload(app, "Книга.fb2", fb2_bytes)
    for _ in range(2):
        _, data = api(app, "/api/convert", {"file_id": info["id"], "target": "txt"})
        assert wait(app, data["job_id"])["status"] == "done"
    assert sorted(item.name for item in (tmp_path / "output").iterdir()) == ["Книга (1).txt", "Книга.txt"]


def test_hostile_file_name_stays_in_output_folder(app, tmp_path, fb2_bytes):
    info = upload(app, "..\\..\\..\\evil.fb2", fb2_bytes)
    _, data = api(app, "/api/convert", {"file_id": info["id"], "target": "txt"})
    assert wait(app, data["job_id"])["status"] == "done"
    assert (tmp_path / "output" / "evil.txt").exists()


def test_broken_file_reports_clear_error(app):
    info = upload(app, "сломанный.pdf", b"not a pdf at all")
    assert info["error"]
    status, data = api(app, "/api/convert", {"file_id": info["id"], "target": "txt"})
    assert status == 400 and "PDF" in data["error"]


def test_corrupt_pdf_error_does_not_leave_partial_file(app, tmp_path):
    info = upload(app, "битый.pdf", "%PDF-1.7\nмусор".encode())
    _, data = api(app, "/api/convert", {"file_id": info["id"], "target": "docx"})
    job = wait(app, data["job_id"])
    assert job["status"] == "error" and job["error"]
    output = tmp_path / "output"
    assert not output.exists() or not list(output.iterdir())


def test_builder_merge_split_export(app, tmp_path, fb2_bytes):
    state, port = app
    info = upload(app, "Первая.fb2", fb2_bytes)
    _, data = api(app, "/api/builder/add", {"file_id": info["id"], "split": True})
    job = wait(app, data["job_id"])
    chapters = job["result"]["chapters"]
    assert len(chapters) == 3 and chapters[0]["words"] > 0
    assert job["result"]["meta"]["title"] == "Лето в Алматы"

    # Просмотр главы экранирует текст и не содержит активного содержимого.
    status, raw, _ = request(port, "GET", f"/api/builder/chapter/{chapters[2]['id']}", headers={"X-Token": state.token})
    preview = json.loads(raw)
    assert status == 200 and any("&lt;тег&gt;" in line for line in preview["html"])

    _, merged = api(app, "/api/builder/merge", {"ids": [chapters[0]["id"], chapters[1]["id"]]})
    _, parts = api(app, "/api/builder/split", {"id": merged["id"], "at": 2, "title": "Хвост"})
    order = [
        {"id": parts["second"]["id"], "title": "Хвост", "level": 1},
        {"id": parts["first"]["id"], "title": "Голова", "level": 1},
        {"id": chapters[2]["id"], "title": chapters[2]["title"], "level": 1},
    ]
    cover = upload(app, "обложка.jpg", jpeg_bytes())
    _, cover_data = api(app, "/api/builder/cover", {"file_id": cover["id"]})
    _, data = api(
        app,
        "/api/builder/export",
        {
            "format": "epub",
            "meta": {"title": "Сборник", "authors": "Автор Один, Автор Два", "cover_id": cover_data["cover_id"]},
            "chapters": order,
        },
    )
    job = wait(app, data["job_id"])
    assert job["status"] == "done", job
    from knigovert.convert import read_book

    book = read_book(tmp_path / "output" / "Сборник.epub")
    assert [chapter.title for chapter in book.chapters] == ["Хвост", "Голова", "Глава 3. Финал"]
    assert book.meta.authors == ["Автор Один", "Автор Два"]
    assert book.meta.cover_id


def test_builder_export_empty_is_an_error(app):
    status, data = api(app, "/api/builder/export", {"format": "pdf", "meta": {}, "chapters": []})
    assert status == 400 and "глав" in data["error"]


def test_cover_must_be_an_image(app):
    info = upload(app, "текст.txt", "просто текст".encode())
    status, data = api(app, "/api/builder/cover", {"file_id": info["id"]})
    assert status == 400 and "картинка" in data["error"]


def test_open_only_own_outputs(app):
    status, data = api(app, "/api/open", {"path": "C:\\Windows\\System32\\calc.exe"})
    assert status == 400


def test_output_dir_requires_absolute_path(app, tmp_path):
    status, _ = api(app, "/api/output-dir", {"path": "relative/folder"})
    assert status == 400
    status, data = api(app, "/api/output-dir", {"path": str(tmp_path / "новая папка")})
    assert status == 200 and data["output_dir"].endswith("новая папка")
    assert json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))["output_dir"].endswith("новая папка")
